"""Gateway 的 Lambda target:售后业务工具。

AgentCore Gateway 的 Lambda 调用契约(已对着官方文档核对):
  event   = inputSchema 里各 property 的扁平 map,例如 {"order_id": "ORD-1024"}
  context = 元数据放在 context.client_context.custom 里,其中
            bedrockAgentCoreToolName 的格式是 "${target_name}___${tool_name}",
            前缀必须自己剥掉。
  返回值  = 必须是 Gateway 能 JSON 序列化的对象。

零第三方依赖,只用标准库 + Lambda 自带 boto3。

信任边界说明:
  这个 Lambda 把 Gateway 当作可信调用方 —— 入向鉴权(CUSTOM_JWT)已经由
  Gateway 做完,未通过的请求到不了这里。所以这里不再做身份校验。
  代价是:它也【不做按用户的数据隔离】,任何通过鉴权的调用方都能查任何订单。
  要做到按用户隔离,需要给 target 配 JWT_PASSTHROUGH 凭证类型 +
  metadataConfiguration.allowedRequestHeaders 把 Authorization 透进来,
  再在这里解 actor_id 过滤。demo 里刻意不做,但这是生产必须补的一环。
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from decimal import Decimal
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

LOG = logging.getLogger()
LOG.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

BUSINESS_TABLE = os.environ["BUSINESS_TABLE"]

# Gateway 给工具名加的前缀分隔符
TOOL_NAME_DELIMITER = "___"

_ddb = boto3.client(
    "dynamodb", config=Config(retries={"max_attempts": 3, "mode": "standard"})
)

# 输入校验:宁可拒掉也不要把奇怪的值带进查询
_ORDER_ID_RE = re.compile(r"^ORD-[0-9A-Za-z]{1,24}$")
_TICKET_ID_RE = re.compile(r"^TKT-[0-9a-f]{8,32}$")
_CUSTOMER_ID_RE = re.compile(r"^CUST-[0-9A-Za-z]{1,24}$")

_MAX_LIMIT = 25
_MAX_SUMMARY_LEN = 500

TICKET_CATEGORIES = ("logistics_delay", "damaged", "wrong_item", "refund", "other")
TICKET_SEVERITIES = ("low", "normal", "high")

# 赔付政策。刻意作为数据返回给模型,让它把规则交给 Code Interpreter 去算,
# 而不是自己心算 —— 这是 demo 想展示的分工。
REFUND_POLICY: dict[str, Any] = {
    "currency": "CNY",
    "rules": [
        {
            "category": "logistics_delay",
            "description": "超出承诺送达时间后,每满 24 小时赔付订单金额的 5%",
            "per_period_hours": 24,
            "rate_per_period": 0.05,
            "max_rate": 0.30,
            "min_payout_cents": 200,
        },
        {
            "category": "damaged",
            "description": "商品破损,全额退款并补偿 10%",
            "rate_per_period": 0.0,
            "base_rate": 1.10,
            "max_rate": 1.10,
            "min_payout_cents": 0,
        },
        {
            "category": "wrong_item",
            "description": "错发商品,全额退款,不额外补偿",
            "base_rate": 1.00,
            "max_rate": 1.00,
            "min_payout_cents": 0,
        },
    ],
    "note": "赔付金额一律按分(cents)计算后四舍五入到整分,不要用浮点直接累加。",
}


class ToolError(Exception):
    """入参或业务校验失败。会被转成结构化错误返回给模型。"""


# ---------------------------------------------------------------------------
# DynamoDB 值转换
# ---------------------------------------------------------------------------


def _from_attr(value: dict[str, Any]) -> Any:
    """DynamoDB 属性值 -> Python。只覆盖本项目用到的类型。"""
    if "S" in value:
        return value["S"]
    if "N" in value:
        number = Decimal(value["N"])
        return int(number) if number == number.to_integral_value() else float(number)
    if "BOOL" in value:
        return value["BOOL"]
    if "NULL" in value:
        return None
    if "L" in value:
        return [_from_attr(v) for v in value["L"]]
    if "M" in value:
        return {k: _from_attr(v) for k, v in value["M"].items()}
    if "SS" in value:
        return list(value["SS"])
    return None


def _item_to_dict(item: dict[str, Any], *, drop_keys: bool = True) -> dict[str, Any]:
    out = {k: _from_attr(v) for k, v in item.items()}
    if drop_keys:
        # 分区键/索引键是存储细节,不要泄露给模型,省 token 也少混淆
        for key in ("PK", "SK", "GSI1PK", "GSI1SK"):
            out.pop(key, None)
    return out


# ---------------------------------------------------------------------------
# 入参校验
# ---------------------------------------------------------------------------


def _require_str(event: dict[str, Any], name: str) -> str:
    value = event.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ToolError(f"缺少必填参数 {name}")
    return value.strip()


def _validate(value: str, pattern: re.Pattern[str], name: str, example: str) -> str:
    if not pattern.match(value):
        raise ToolError(f"{name} 格式不对:{value!r},应形如 {example}")
    return value


def _clamp_limit(event: dict[str, Any], default: int = 10) -> int:
    raw = event.get("limit", default)
    try:
        limit = int(raw)
    except (TypeError, ValueError):
        raise ToolError(f"limit 必须是整数,收到 {raw!r}") from None
    return max(1, min(limit, _MAX_LIMIT))


# ---------------------------------------------------------------------------
# 工具实现
# ---------------------------------------------------------------------------


def tool_get_order(event: dict[str, Any]) -> dict[str, Any]:
    order_id = _validate(
        _require_str(event, "order_id"), _ORDER_ID_RE, "order_id", "ORD-1024"
    )
    resp = _ddb.get_item(
        TableName=BUSINESS_TABLE,
        Key={"PK": {"S": f"ORDER#{order_id}"}, "SK": {"S": "META"}},
    )
    item = resp.get("Item")
    if not item:
        raise ToolError(f"没有找到订单 {order_id}")

    order = _item_to_dict(item)
    now = int(time.time())
    promised_at = order.get("promised_at")
    if isinstance(promised_at, int) and order.get("status") != "delivered":
        overdue = now - promised_at
        # 把"超期多久"算好给模型,省得它自己做时间戳减法出错
        order["is_overdue"] = overdue > 0
        order["overdue_hours"] = round(max(overdue, 0) / 3600, 2)
    return order


def tool_list_orders_by_customer(event: dict[str, Any]) -> dict[str, Any]:
    customer_id = _validate(
        _require_str(event, "customer_id"), _CUSTOMER_ID_RE, "customer_id", "CUST-001"
    )
    limit = _clamp_limit(event)
    resp = _ddb.query(
        TableName=BUSINESS_TABLE,
        IndexName="GSI1",
        KeyConditionExpression="GSI1PK = :pk AND begins_with(GSI1SK, :prefix)",
        ExpressionAttributeValues={
            ":pk": {"S": f"CUSTOMER#{customer_id}"},
            ":prefix": {"S": "ORDER#"},
        },
        ScanIndexForward=False,
        Limit=limit,
    )
    orders = [_item_to_dict(item) for item in resp.get("Items") or []]
    return {"customer_id": customer_id, "count": len(orders), "orders": orders}


def tool_create_ticket(event: dict[str, Any]) -> dict[str, Any]:
    order_id = _validate(
        _require_str(event, "order_id"), _ORDER_ID_RE, "order_id", "ORD-1024"
    )
    category = _require_str(event, "category")
    if category not in TICKET_CATEGORIES:
        raise ToolError(f"category 必须是 {list(TICKET_CATEGORIES)} 之一,收到 {category!r}")
    summary = _require_str(event, "summary")[:_MAX_SUMMARY_LEN]
    severity = str(event.get("severity") or "normal")
    if severity not in TICKET_SEVERITIES:
        raise ToolError(f"severity 必须是 {list(TICKET_SEVERITIES)} 之一,收到 {severity!r}")

    # 开工单前先确认订单存在,免得产生孤儿工单
    order = _ddb.get_item(
        TableName=BUSINESS_TABLE,
        Key={"PK": {"S": f"ORDER#{order_id}"}, "SK": {"S": "META"}},
        ProjectionExpression="PK",
    ).get("Item")
    if not order:
        raise ToolError(f"订单 {order_id} 不存在,无法开工单")

    ticket_id = f"TKT-{uuid.uuid4().hex[:12]}"
    now = int(time.time())
    item = {
        "PK": {"S": f"TICKET#{ticket_id}"},
        "SK": {"S": "META"},
        "GSI1PK": {"S": f"ORDER#{order_id}"},
        "GSI1SK": {"S": f"TICKET#{now:012d}#{ticket_id}"},
        "ticket_id": {"S": ticket_id},
        "order_id": {"S": order_id},
        "category": {"S": category},
        "severity": {"S": severity},
        "summary": {"S": summary},
        "status": {"S": "open"},
        "created_at": {"N": str(now)},
    }
    try:
        _ddb.put_item(
            TableName=BUSINESS_TABLE,
            Item=item,
            # uuid 撞车概率极低,但写操作加条件是廉价的保险
            ConditionExpression="attribute_not_exists(PK)",
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            raise ToolError("工单号冲突,请重试") from None
        raise

    LOG.info("created ticket %s for order %s category=%s", ticket_id, order_id, category)
    return {
        "ticket_id": ticket_id,
        "order_id": order_id,
        "category": category,
        "severity": severity,
        "status": "open",
        "created_at": now,
    }


def tool_list_tickets(event: dict[str, Any]) -> dict[str, Any]:
    order_id = _validate(
        _require_str(event, "order_id"), _ORDER_ID_RE, "order_id", "ORD-1024"
    )
    limit = _clamp_limit(event)
    resp = _ddb.query(
        TableName=BUSINESS_TABLE,
        IndexName="GSI1",
        KeyConditionExpression="GSI1PK = :pk AND begins_with(GSI1SK, :prefix)",
        ExpressionAttributeValues={
            ":pk": {"S": f"ORDER#{order_id}"},
            ":prefix": {"S": "TICKET#"},
        },
        ScanIndexForward=False,
        Limit=limit,
    )
    tickets = [_item_to_dict(item) for item in resp.get("Items") or []]
    return {"order_id": order_id, "count": len(tickets), "tickets": tickets}


def tool_get_refund_policy(event: dict[str, Any]) -> dict[str, Any]:
    category = event.get("category")
    if not category:
        return REFUND_POLICY
    matched = [r for r in REFUND_POLICY["rules"] if r["category"] == category]
    if not matched:
        raise ToolError(
            f"没有 {category} 的赔付规则,可用:{[r['category'] for r in REFUND_POLICY['rules']]}"
        )
    return {"currency": REFUND_POLICY["currency"], "rules": matched,
            "note": REFUND_POLICY["note"]}


TOOLS: dict[str, Any] = {
    "get_order": tool_get_order,
    "list_orders_by_customer": tool_list_orders_by_customer,
    "create_ticket": tool_create_ticket,
    "list_tickets": tool_list_tickets,
    "get_refund_policy": tool_get_refund_policy,
}


# ---------------------------------------------------------------------------
# 工具 schema
#
# 和实现放同一个文件,靠 tests/test_business_tools.py 断言两边不漂移。
# scripts/create_gateway.py 直接 import 这个常量喂给 CreateGatewayTarget 的
# targetConfiguration.mcp.lambda.toolSchema.inlinePayload。
#
# description 要写得足够具体:中国区 Gateway 没有语义检索
# (searchType=SEMANTIC 不可用),模型完全靠这段文字决定调哪个工具。
# ---------------------------------------------------------------------------

TOOL_SCHEMA: list[dict[str, Any]] = [
    {
        "name": "get_order",
        "description": (
            "按订单号查询单个订单的完整信息:状态、金额(分)、下单时间、"
            "承诺送达时间、运单号和承运商。若订单未签收且已超过承诺送达时间,"
            "还会额外返回 is_overdue 和 overdue_hours(已超期小时数)。"
            "处理任何与具体订单相关的问题都应先调用它。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "order_id": {
                    "type": "string",
                    "description": "订单号,形如 ORD-1024",
                }
            },
            "required": ["order_id"],
        },
    },
    {
        "name": "list_orders_by_customer",
        "description": (
            "列出某个客户最近的订单,按下单时间倒序。"
            "用于用户没给订单号、只说「我上次买的那个」时定位订单。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "customer_id": {
                    "type": "string",
                    "description": "客户号,形如 CUST-001",
                },
                "limit": {
                    "type": "integer",
                    "description": f"返回条数,1-{_MAX_LIMIT},默认 10",
                },
            },
            "required": ["customer_id"],
        },
    },
    {
        "name": "create_ticket",
        "description": (
            "为某个订单创建售后工单。这是一个写操作,会真实落库,"
            "调用前必须先确认订单号无误、且已经向用户说明要开工单。"
            "返回工单号 ticket_id。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string", "description": "订单号,形如 ORD-1024"},
                "category": {
                    "type": "string",
                    "description": (
                        "工单类型,必须是以下之一:"
                        "logistics_delay(物流延迟)、damaged(商品破损)、"
                        "wrong_item(错发商品)、refund(退款)、other(其他)"
                    ),
                },
                "summary": {
                    "type": "string",
                    "description": f"问题摘要,一两句话,最多 {_MAX_SUMMARY_LEN} 字",
                },
                "severity": {
                    "type": "string",
                    "description": "严重程度:low / normal / high,默认 normal",
                },
            },
            "required": ["order_id", "category", "summary"],
        },
    },
    {
        "name": "list_tickets",
        "description": (
            "列出某个订单已有的工单,按创建时间倒序。"
            "开新工单前应先调它,避免为同一个问题重复开单。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string", "description": "订单号,形如 ORD-1024"},
                "limit": {
                    "type": "integer",
                    "description": f"返回条数,1-{_MAX_LIMIT},默认 10",
                },
            },
            "required": ["order_id"],
        },
    },
    {
        "name": "get_refund_policy",
        "description": (
            "查询赔付政策规则,返回费率、周期、上限和最低赔付额。"
            "这个工具只给规则,不算钱 —— 拿到规则后必须用 run_python "
            "在沙箱里按分(cents)计算具体金额,不要自己心算。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "description": (
                        "只看某一类的规则,可选。取值同 create_ticket 的 category。"
                        "不传则返回全部规则。"
                    ),
                }
            },
            "required": [],
        },
    },
]


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def extract_tool_name(context: Any) -> str:
    """从 Gateway 传来的 context 里取工具名并剥掉 target 前缀。

    Gateway 给的形式是 "${target_name}___${tool_name}"。
    """
    custom = getattr(getattr(context, "client_context", None), "custom", None) or {}
    raw = custom.get("bedrockAgentCoreToolName", "")
    if not raw:
        raise ToolError("context 里没有 bedrockAgentCoreToolName,这个函数只能由 Gateway 调用")
    if TOOL_NAME_DELIMITER in raw:
        return raw.split(TOOL_NAME_DELIMITER, 1)[1]
    return raw


def lambda_handler(event: dict[str, Any], context: Any) -> Any:
    try:
        tool_name = extract_tool_name(context)
    except ToolError as exc:
        LOG.warning("%s", exc)
        return {"error": str(exc)}

    handler = TOOLS.get(tool_name)
    if handler is None:
        LOG.warning("unknown tool %r", tool_name)
        return {"error": f"未知工具 {tool_name},可用:{sorted(TOOLS)}"}

    if not isinstance(event, dict):
        return {"error": "event 必须是一个对象"}

    LOG.info("tool=%s args=%s", tool_name, json.dumps(sorted(event.keys())))
    try:
        return handler(event)
    except ToolError as exc:
        # 校验类错误原文返回:模型看到后能自己纠正参数重试
        return {"error": str(exc)}
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "Unknown")
        LOG.exception("DynamoDB 调用失败 tool=%s", tool_name)
        return {"error": f"后端数据访问失败({code}),请稍后重试"}
    except Exception:  # noqa: BLE001
        # 内部细节只进日志,不回给模型(它会原样转述给用户)
        LOG.exception("工具 %s 执行失败", tool_name)
        return {"error": "工具内部错误,请联系管理员查看日志"}
