"""MemoryLite:用 DynamoDB 补位中国区缺失的 AgentCore Memory。

中国区不提供 AgentCore Memory,但会话记忆是 Agent 的刚性需求,
所以自己实现一个够用的版本:

  短期记忆 STM  按 session 存对话轮次,带 TTL 自动过期。
                这同时也是 Runtime "会话隔离" 的落点 —— 同一个 actor
                的不同 sessionId 读不到彼此的消息。
  会话摘要      超出窗口的历史不是直接丢掉,而是压成一段摘要留着,
                否则长会话会突然"失忆"。
  长期记忆 LTM  按 actor 存结构化事实(偏好、历史结论),不设 TTL。

表结构(单表,复用 ${PROJECT}-memory):
  STM   PK=SESSION#<session_id>   SK=MSG#<零填充序号>   expires_at=TTL
  摘要  PK=SESSION#<session_id>   SK=SUMMARY           expires_at=TTL
  LTM   PK=ACTOR#<actor_id>       SK=FACT#<key>        无 TTL

SK 用零填充序号而不是时间戳:同一毫秒内的多条消息用时间戳会撞键,
而且字典序必须等于时间序,Query 才能直接按顺序取回。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Literal

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from agent import obs
from agent.config import Settings, get_settings

LOG = logging.getLogger(__name__)

Role = Literal["user", "assistant"]

# 序号宽度。12 位十进制足够任何会话,且保证字典序 == 数值序
_SEQ_WIDTH = 12

# 单条消息的长度上限。DynamoDB 单条记录上限 400KB,中文按 UTF-8 最多 3 字节/字,
# 2 万字约 60KB,留足余量。超了就截断 —— 让整次写入因为超限而失败更糟。
MAX_CONTENT_CHARS = 20_000

# 一条长期事实的长度上限,以及一个 actor 最多能存多少条。
# 不设上限的话模型可以把整段对话当"偏好"反复写进去。
MAX_FACT_CHARS = 500
MAX_FACTS_PER_ACTOR = 50

# 摘要本身的长度上限
MAX_SUMMARY_CHARS = 2_000

# 序号冲突时的重试次数。无界递归重试会栈溢出。
_SEQ_CONFLICT_RETRIES = 5

_TRUNCATION_MARK = "…[已截断]"


class MemoryLimitExceeded(RuntimeError):
    """写入被长度/数量限制拒绝。"""


@dataclass(frozen=True)
class Turn:
    role: Role
    content: str
    created_at: int


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - len(_TRUNCATION_MARK)] + _TRUNCATION_MARK


class MemoryLite:
    def __init__(self, settings: Settings | None = None, *, client: Any = None) -> None:
        self._settings = settings or get_settings()
        self._ddb = client or boto3.client(
            "dynamodb",
            region_name=self._settings.region,
            config=Config(retries={"max_attempts": 3, "mode": "standard"}),
        )
        self._table = self._settings.memory_table

    # ------------------------------------------------------------------
    # 短期记忆
    # ------------------------------------------------------------------

    @staticmethod
    def _session_pk(session_id: str) -> str:
        return f"SESSION#{session_id}"

    def _ttl(self, now: int) -> int:
        return now + self._settings.stm_ttl_seconds

    def _next_seq(self, session_id: str) -> int:
        """取当前最大序号 + 1。

        用 ADD 原子计数器会更稳,但那要多一次写;demo 场景单会话串行,
        倒序取一条已经够用,而且省一次 RCU。冲突由 append_turn 的
        条件写 + 有界重试兜住。
        """
        resp = self._ddb.query(
            TableName=self._table,
            KeyConditionExpression="PK = :pk AND begins_with(SK, :prefix)",
            ExpressionAttributeValues={
                ":pk": {"S": self._session_pk(session_id)},
                ":prefix": {"S": "MSG#"},
            },
            ScanIndexForward=False,
            Limit=1,
            ProjectionExpression="SK",
        )
        items = resp.get("Items") or []
        if not items:
            return 0
        return int(items[0]["SK"]["S"].removeprefix("MSG#")) + 1

    def append_turn(self, session_id: str, role: Role, content: str) -> None:
        """追加一条消息。

        并发写同一序号时靠条件写挡住,然后重新取号重试 ——
        **有界重试**,不是递归。早先的实现是自己递归调用,
        持续冲突会一路递归到栈溢出。
        """
        if not session_id:
            LOG.warning("没有 session_id,跳过 STM 写入")
            return

        # 超长内容截断而不是让整次写入失败
        body = _truncate(content or "", MAX_CONTENT_CHARS)
        if len(content or "") > MAX_CONTENT_CHARS:
            LOG.warning(
                "消息过长(%d 字符),已截断到 %d", len(content), MAX_CONTENT_CHARS
            )

        with obs.span("memory.append_turn", role=role, content_len=len(body)):
            self._append_with_retry(session_id, role, body)

    def _append_with_retry(self, session_id: str, role: Role, body: str) -> None:
        for attempt in range(_SEQ_CONFLICT_RETRIES):
            now = int(time.time())
            seq = self._next_seq(session_id)
            try:
                self._ddb.put_item(
                    TableName=self._table,
                    Item={
                        "PK": {"S": self._session_pk(session_id)},
                        "SK": {"S": f"MSG#{seq:0{_SEQ_WIDTH}d}"},
                        "role": {"S": role},
                        "content": {"S": body},
                        "created_at": {"N": str(now)},
                        "expires_at": {"N": str(self._ttl(now))},
                    },
                    ConditionExpression="attribute_not_exists(SK)",
                )
                return
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code")
                if code != "ConditionalCheckFailedException":
                    raise
                LOG.warning(
                    "STM 序号 %d 冲突,重试 %d/%d", seq, attempt + 1, _SEQ_CONFLICT_RETRIES
                )
        # 重试用尽:宁可丢一条历史,也不要让整轮对话失败
        LOG.error("STM 写入连续冲突 %d 次,放弃这条消息", _SEQ_CONFLICT_RETRIES)

    def _query_messages(self, session_id: str, limit: int) -> list[dict[str, Any]]:
        resp = self._ddb.query(
            TableName=self._table,
            KeyConditionExpression="PK = :pk AND begins_with(SK, :prefix)",
            ExpressionAttributeValues={
                ":pk": {"S": self._session_pk(session_id)},
                ":prefix": {"S": "MSG#"},
            },
            # 倒序取最近若干条,再翻回正序
            ScanIndexForward=False,
            Limit=limit,
        )
        return resp.get("Items") or []

    def load_history(self, session_id: str, *, max_turns: int | None = None) -> list[Turn]:
        """按时间正序取回最近若干轮对话。

        max_turns 指的是**轮**(一问一答算一轮),所以底层要取 2 倍条数。
        返回的列表保证以 user 消息开头 —— 窗口边界可能正好切在
        assistant 消息上,而部分 OpenAI 兼容端点收到以 assistant 开头的
        历史会直接 400。
        """
        if not session_id:
            return []
        max_turns = max_turns or self._settings.stm_max_turns
        with obs.span("memory.load_history", max_turns=max_turns) as sp:
            turns = self._load_history_inner(session_id, max_turns)
            sp["turn_count"] = len(turns)
            return turns

    def _load_history_inner(self, session_id: str, max_turns: int) -> list[Turn]:
        items = self._query_messages(session_id, limit=max_turns * 2)

        now = int(time.time())
        turns: list[Turn] = []
        for item in items:
            # DynamoDB 的 TTL 清理最长可能延迟 48 小时,自己再过滤一次
            if int(item.get("expires_at", {}).get("N", "0")) <= now:
                continue
            turns.append(
                Turn(
                    role=item["role"]["S"],  # type: ignore[arg-type]
                    content=item["content"]["S"],
                    created_at=int(item["created_at"]["N"]),
                )
            )
        turns.reverse()

        # 丢掉开头的 assistant 消息,保证历史从 user 开始
        while turns and turns[0].role != "user":
            turns.pop(0)
        return turns

    def count_messages(self, session_id: str) -> int:
        """这个会话一共存了多少条消息(用于判断要不要做摘要)。"""
        if not session_id:
            return 0
        resp = self._ddb.query(
            TableName=self._table,
            KeyConditionExpression="PK = :pk AND begins_with(SK, :prefix)",
            ExpressionAttributeValues={
                ":pk": {"S": self._session_pk(session_id)},
                ":prefix": {"S": "MSG#"},
            },
            Select="COUNT",
        )
        return int(resp.get("Count", 0))

    def as_strands_messages(self, session_id: str) -> list[dict[str, Any]]:
        """转成 Strands Agent 构造函数吃的 messages 格式。"""
        return [
            {"role": turn.role, "content": [{"text": turn.content}]}
            for turn in self.load_history(session_id)
        ]

    # ------------------------------------------------------------------
    # 会话摘要
    #
    # 超出窗口的历史不是直接丢掉 —— 那会让长会话突然"失忆"。
    # 压成一段摘要,由 assembly 拼进 system prompt。
    # ------------------------------------------------------------------

    def set_summary(self, session_id: str, summary: str, *, covered_upto: int) -> None:
        """写入/覆盖会话摘要。

        covered_upto 记录这段摘要覆盖到第几条消息,
        下次判断"要不要重新摘要"时靠它,避免重复花钱。
        """
        if not session_id or not summary.strip():
            return
        now = int(time.time())
        self._ddb.put_item(
            TableName=self._table,
            Item={
                "PK": {"S": self._session_pk(session_id)},
                "SK": {"S": "SUMMARY"},
                "summary": {"S": _truncate(summary.strip(), MAX_SUMMARY_CHARS)},
                "covered_upto": {"N": str(covered_upto)},
                "updated_at": {"N": str(now)},
                "expires_at": {"N": str(self._ttl(now))},
            },
        )

    def get_summary(self, session_id: str) -> tuple[str, int]:
        """返回 (摘要文本, 覆盖到第几条)。没有摘要则返回 ("", 0)。"""
        if not session_id:
            return "", 0
        resp = self._ddb.get_item(
            TableName=self._table,
            Key={"PK": {"S": self._session_pk(session_id)}, "SK": {"S": "SUMMARY"}},
        )
        item = resp.get("Item")
        if not item:
            return "", 0
        if int(item.get("expires_at", {}).get("N", "0")) <= int(time.time()):
            return "", 0
        return item["summary"]["S"], int(item.get("covered_upto", {}).get("N", "0"))

    def needs_summary(self, session_id: str) -> bool:
        """消息数超出窗口、且现有摘要已经跟不上时,才值得重新摘要。"""
        total = self.count_messages(session_id)
        window = self._settings.stm_max_turns * 2
        if total <= window:
            return False
        _, covered = self.get_summary(session_id)
        # 摘要至少要覆盖到"窗口之外"的那部分
        return covered < total - window

    def messages_outside_window(self, session_id: str) -> list[Turn]:
        """取出窗口之外(即将被摘要的)那部分历史。"""
        if not session_id:
            return []
        window = self._settings.stm_max_turns * 2
        # 取全量再切,demo 会话不长;真实场景该改成分页 + 只取需要的区间
        items = self._query_messages(session_id, limit=window * 10)
        items.reverse()  # 正序
        older = items[: max(0, len(items) - window)]
        return [
            Turn(
                role=i["role"]["S"],  # type: ignore[arg-type]
                content=i["content"]["S"],
                created_at=int(i["created_at"]["N"]),
            )
            for i in older
        ]

    # ------------------------------------------------------------------
    # 长期记忆
    # ------------------------------------------------------------------

    @staticmethod
    def _actor_pk(actor_id: str) -> str:
        return f"ACTOR#{actor_id}"

    def put_fact(self, actor_id: str, key: str, value: str) -> None:
        """写入或覆盖一条长期事实。不设 TTL。

        有数量上限:覆盖已有 key 总是允许,新增 key 超过上限就拒绝 ——
        否则模型可以把整段对话当"偏好"无限写进去。
        """
        if not actor_id or not key:
            return
        existing = self.get_facts(actor_id)
        if key not in existing and len(existing) >= MAX_FACTS_PER_ACTOR:
            raise MemoryLimitExceeded(
                f"这个用户的长期记忆已达上限({MAX_FACTS_PER_ACTOR} 条),"
                "先用 forget 删掉不需要的再写。"
            )
        self._ddb.put_item(
            TableName=self._table,
            Item={
                "PK": {"S": self._actor_pk(actor_id)},
                "SK": {"S": f"FACT#{key}"},
                "value": {"S": _truncate(value, MAX_FACT_CHARS)},
                "updated_at": {"N": str(int(time.time()))},
            },
        )

    def get_facts(self, actor_id: str) -> dict[str, str]:
        """取出某个 actor 的全部长期事实。

        分页拉完 —— Query 单次最多返回 1MB,不翻页会静默丢数据。
        """
        if not actor_id:
            return {}
        facts: dict[str, str] = {}
        start_key: dict[str, Any] | None = None
        while True:
            kwargs: dict[str, Any] = {
                "TableName": self._table,
                "KeyConditionExpression": "PK = :pk AND begins_with(SK, :prefix)",
                "ExpressionAttributeValues": {
                    ":pk": {"S": self._actor_pk(actor_id)},
                    ":prefix": {"S": "FACT#"},
                },
            }
            if start_key:
                kwargs["ExclusiveStartKey"] = start_key
            resp = self._ddb.query(**kwargs)
            for item in resp.get("Items") or []:
                facts[item["SK"]["S"].removeprefix("FACT#")] = item["value"]["S"]
            start_key = resp.get("LastEvaluatedKey")
            if not start_key:
                return facts

    def delete_fact(self, actor_id: str, key: str) -> None:
        if not actor_id or not key:
            return
        self._ddb.delete_item(
            TableName=self._table,
            Key={
                "PK": {"S": self._actor_pk(actor_id)},
                "SK": {"S": f"FACT#{key}"},
            },
        )


__all__ = [
    "MemoryLite",
    "MemoryLimitExceeded",
    "Turn",
    "MAX_CONTENT_CHARS",
    "MAX_FACT_CHARS",
    "MAX_FACTS_PER_ACTOR",
    "MAX_SUMMARY_CHARS",
]
