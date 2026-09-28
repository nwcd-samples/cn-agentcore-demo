"""暴露给模型的长期记忆读写工具。

短期记忆不需要工具 —— 它在每轮开始时已经被塞进 messages 了。
长期记忆需要工具,因为写入必须由模型显式决定(用户说了句偏好才记),
不能每轮无条件写。
"""

from __future__ import annotations

import logging

from strands import tool

from agent.memory_lite import MAX_FACT_CHARS, MemoryLimitExceeded, MemoryLite

LOG = logging.getLogger(__name__)

# 防止模型把整段对话当成"偏好"塞进去。
# 长度上限与 MemoryLite 共用同一个常量,避免两边漂移。
_MAX_FACT_LEN = MAX_FACT_CHARS
_MAX_KEY_LEN = 64


def build_memory_tools_for(memory: MemoryLite, actor_id: str) -> list:
    """构造绑定到某个 actor 的记忆工具。

    actor_id 通过闭包固定,不暴露成工具参数 —— 否则模型可以指定任意
    actor_id 去读写别人的数据,这是个真实的越权面。
    """

    @tool
    def remember(key: str, value: str) -> str:
        """记住一条关于当前用户的长期偏好或事实,跨会话有效。

        适合记:通知方式偏好、方便联系的时段、常用收货地址别名。
        不要记:一次性的订单号、临时问题描述、整段对话内容。

        Args:
            key: 简短的英文或拼音标识,例如 notify_channel、contact_window。
            value: 要记住的内容,一句话以内。
        """
        key = (key or "").strip()[:_MAX_KEY_LEN]
        value = (value or "").strip()[:_MAX_FACT_LEN]
        if not key or not value:
            return "记忆失败:key 和 value 都不能为空。"
        try:
            memory.put_fact(actor_id, key, value)
        except MemoryLimitExceeded as exc:
            # 这是预期内的拒绝,原文返回让模型知道该先 forget
            return str(exc)
        except Exception as exc:  # noqa: BLE001
            LOG.exception("写长期记忆失败")
            return f"记忆失败:{type(exc).__name__}"
        return f"已记住 {key}={value}"

    @tool
    def recall() -> str:
        """列出当前用户已记住的全部长期偏好。"""
        try:
            facts = memory.get_facts(actor_id)
        except Exception as exc:  # noqa: BLE001
            LOG.exception("读长期记忆失败")
            return f"读取失败:{type(exc).__name__}"
        if not facts:
            return "这个用户还没有记录任何长期偏好。"
        return "\n".join(f"{k}: {v}" for k, v in sorted(facts.items()))

    @tool
    def forget(key: str) -> str:
        """删除一条长期偏好。

        Args:
            key: 要删除的标识,可先用 recall 查看有哪些。
        """
        key = (key or "").strip()[:_MAX_KEY_LEN]
        if not key:
            return "删除失败:key 不能为空。"
        try:
            memory.delete_fact(actor_id, key)
        except Exception as exc:  # noqa: BLE001
            LOG.exception("删除长期记忆失败")
            return f"删除失败:{type(exc).__name__}"
        return f"已删除 {key}"

    return [remember, recall, forget]
