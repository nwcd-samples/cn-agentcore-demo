"""业务工具 Lambda + Gateway 接线的测试。

分两块:
  TestBusinessTools*  Lambda 本身的逻辑,用假 DynamoDB
  TestGatewayWiring   用 botocore 的参数校验器离线验证 CreateGateway /
                      CreateGatewayTarget 的请求形状。这一块很值 ——
                      Gateway 建栈失败的报错通常很含糊,而参数拼错
                      (少个必填字段、嵌套层级错、用了中国区不支持的枚举)
                      是最常见的原因。不需要任何凭证就能验。
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest


# ---------------------------------------------------------------------------
# 装置
# ---------------------------------------------------------------------------


def gateway_context(tool_name: str, *, target: str = "business") -> SimpleNamespace:
    """伪造 Gateway 传给 Lambda 的 context 对象。

    真实形状:context.client_context.custom['bedrockAgentCoreToolName']
    且值带 "<target>___" 前缀。
    """
    return SimpleNamespace(
        client_context=SimpleNamespace(
            custom={
                "bedrockAgentCoreMessageVersion": "1.0",
                "bedrockAgentCoreAwsRequestId": "req-1",
                "bedrockAgentCoreMcpMessageId": "msg-1",
                "bedrockAgentCoreGatewayId": "gw-1",
                "bedrockAgentCoreTargetId": "tg-1",
                "bedrockAgentCoreToolName": f"{target}___{tool_name}"
                if target
                else tool_name,
            }
        )
    )


@pytest.fixture
def tools(monkeypatch, fake_ddb_factory):
    """返回 (handler 模块, 假 DDB)。"""
    import tools_handler as handler

    fake = fake_ddb_factory()
    monkeypatch.setattr(handler, "_ddb", fake)
    return handler, fake


def seed_order(
    fake,
    order_id="ORD-1024",
    customer_id="CUST-001",
    status="shipped",
    amount_cents=129900,
    created_at=None,
    promised_at=None,
    **extra,
):
    now = int(time.time())
    created_at = created_at if created_at is not None else now - 9 * 86400
    promised_at = promised_at if promised_at is not None else now - 3 * 86400
    item = {
        "PK": {"S": f"ORDER#{order_id}"},
        "SK": {"S": "META"},
        "GSI1PK": {"S": f"CUSTOMER#{customer_id}"},
        "GSI1SK": {"S": f"ORDER#{created_at:012d}#{order_id}"},
        "order_id": {"S": order_id},
        "customer_id": {"S": customer_id},
        "status": {"S": status},
        "amount_cents": {"N": str(amount_cents)},
        "created_at": {"N": str(created_at)},
        "promised_at": {"N": str(promised_at)},
    }
    for key, value in extra.items():
        item[key] = {"S": str(value)}
    fake.put_item(TableName="t", Item=item)
    return order_id


# ---------------------------------------------------------------------------
# 工具名解析
# ---------------------------------------------------------------------------


class TestToolNameExtraction:
    def test_strips_target_prefix(self, tools):
        handler, _ = tools
        assert handler.extract_tool_name(gateway_context("get_order")) == "get_order"

    def test_works_without_prefix(self, tools):
        """没有 target 前缀时原样返回,别把工具名吃掉。"""
        handler, _ = tools
        assert handler.extract_tool_name(gateway_context("get_order", target="")) == "get_order"

    def test_only_first_delimiter_is_consumed(self, tools):
        """工具名本身含 ___ 时不能被切碎。"""
        handler, _ = tools
        ctx = gateway_context("weird___name")
        assert handler.extract_tool_name(ctx) == "weird___name"

    def test_missing_context_is_rejected(self, tools):
        handler, _ = tools
        with pytest.raises(handler.ToolError):
            handler.extract_tool_name(SimpleNamespace(client_context=None))

    def test_unknown_tool_returns_error(self, tools):
        handler, _ = tools
        result = handler.lambda_handler({}, gateway_context("drop_all_tables"))
        assert "error" in result
        assert "未知工具" in result["error"]


# ---------------------------------------------------------------------------
# get_order
# ---------------------------------------------------------------------------


class TestGetOrder:
    def test_returns_order_and_hides_storage_keys(self, tools):
        handler, fake = tools
        seed_order(fake, shipment_no="SF7758291046", carrier="顺丰")

        result = handler.lambda_handler({"order_id": "ORD-1024"}, gateway_context("get_order"))

        assert result["order_id"] == "ORD-1024"
        assert result["amount_cents"] == 129900
        assert result["shipment_no"] == "SF7758291046"
        # 分区键是存储细节,不该出现在模型看到的结果里
        for key in ("PK", "SK", "GSI1PK", "GSI1SK"):
            assert key not in result

    def test_computes_overdue_hours(self, tools):
        """超期小时数由 Lambda 算好,不让模型做时间戳减法。"""
        handler, fake = tools
        now = int(time.time())
        seed_order(fake, status="shipped", promised_at=now - 30 * 3600)

        result = handler.lambda_handler({"order_id": "ORD-1024"}, gateway_context("get_order"))

        assert result["is_overdue"] is True
        assert 29.9 < result["overdue_hours"] < 30.1

    def test_not_overdue_yet(self, tools):
        handler, fake = tools
        now = int(time.time())
        seed_order(fake, status="pending", promised_at=now + 2 * 86400)

        result = handler.lambda_handler({"order_id": "ORD-1024"}, gateway_context("get_order"))

        assert result["is_overdue"] is False
        assert result["overdue_hours"] == 0

    def test_delivered_order_has_no_overdue_fields(self, tools):
        handler, fake = tools
        seed_order(fake, status="delivered")

        result = handler.lambda_handler({"order_id": "ORD-1024"}, gateway_context("get_order"))

        assert "is_overdue" not in result

    def test_missing_order(self, tools):
        handler, _ = tools
        result = handler.lambda_handler(
            {"order_id": "ORD-9999"}, gateway_context("get_order")
        )
        assert "没有找到订单" in result["error"]

    def test_missing_required_param(self, tools):
        handler, _ = tools
        result = handler.lambda_handler({}, gateway_context("get_order"))
        assert "order_id" in result["error"]

    @pytest.mark.parametrize(
        "bad",
        ["1024", "ORD_1024", "ORD-", "ORD-1024; DROP", "../ORD-1024", "ORD-" + "x" * 30],
    )
    def test_rejects_malformed_order_id(self, tools, bad):
        handler, _ = tools
        result = handler.lambda_handler({"order_id": bad}, gateway_context("get_order"))
        assert "格式不对" in result["error"]


# ---------------------------------------------------------------------------
# list_orders_by_customer
# ---------------------------------------------------------------------------


class TestListOrders:
    def test_returns_newest_first(self, tools):
        handler, fake = tools
        now = int(time.time())
        for i, offset in enumerate([-5 * 86400, -1 * 86400, -9 * 86400]):
            seed_order(fake, order_id=f"ORD-10{i}", created_at=now + offset)

        result = handler.lambda_handler(
            {"customer_id": "CUST-001"}, gateway_context("list_orders_by_customer")
        )

        assert result["count"] == 3
        ids = [o["order_id"] for o in result["orders"]]
        assert ids == ["ORD-101", "ORD-100", "ORD-102"]

    def test_limit_is_clamped(self, tools):
        handler, fake = tools
        for i in range(5):
            seed_order(fake, order_id=f"ORD-20{i}", created_at=int(time.time()) - i * 3600)

        result = handler.lambda_handler(
            {"customer_id": "CUST-001", "limit": 999},
            gateway_context("list_orders_by_customer"),
        )
        assert result["count"] <= handler._MAX_LIMIT

    def test_limit_below_one_is_clamped_up(self, tools):
        handler, fake = tools
        seed_order(fake)
        result = handler.lambda_handler(
            {"customer_id": "CUST-001", "limit": 0},
            gateway_context("list_orders_by_customer"),
        )
        assert result["count"] == 1

    def test_non_numeric_limit_is_reported(self, tools):
        handler, fake = tools
        seed_order(fake)
        result = handler.lambda_handler(
            {"customer_id": "CUST-001", "limit": "many"},
            gateway_context("list_orders_by_customer"),
        )
        assert "limit" in result["error"]

    def test_other_customers_orders_are_not_returned(self, tools):
        handler, fake = tools
        seed_order(fake, order_id="ORD-1024", customer_id="CUST-001")
        seed_order(fake, order_id="ORD-2048", customer_id="CUST-002")

        result = handler.lambda_handler(
            {"customer_id": "CUST-001"}, gateway_context("list_orders_by_customer")
        )
        assert [o["order_id"] for o in result["orders"]] == ["ORD-1024"]


# ---------------------------------------------------------------------------
# create_ticket
# ---------------------------------------------------------------------------


class TestCreateTicket:
    def test_happy_path(self, tools):
        handler, fake = tools
        seed_order(fake)

        result = handler.lambda_handler(
            {
                "order_id": "ORD-1024",
                "category": "logistics_delay",
                "summary": "已超期 3 天未送达",
            },
            gateway_context("create_ticket"),
        )

        assert result["ticket_id"].startswith("TKT-")
        assert result["status"] == "open"
        assert result["severity"] == "normal"
        # 真的落库了
        stored = fake.items[(f"TICKET#{result['ticket_id']}", "META")]
        assert stored["order_id"]["S"] == "ORD-1024"
        assert stored["GSI1PK"]["S"] == "ORDER#ORD-1024"

    def test_refuses_orphan_ticket(self, tools):
        """订单不存在就不该产生工单。"""
        handler, fake = tools
        result = handler.lambda_handler(
            {"order_id": "ORD-9999", "category": "refund", "summary": "x"},
            gateway_context("create_ticket"),
        )
        assert "不存在" in result["error"]
        assert not [k for k in fake.items if k[0].startswith("TICKET#")]

    @pytest.mark.parametrize("category", ["", "sql_injection", "DELAY", "物流延迟"])
    def test_rejects_unknown_category(self, tools, category):
        handler, fake = tools
        seed_order(fake)
        result = handler.lambda_handler(
            {"order_id": "ORD-1024", "category": category, "summary": "x"},
            gateway_context("create_ticket"),
        )
        assert "error" in result

    def test_rejects_unknown_severity(self, tools):
        handler, fake = tools
        seed_order(fake)
        result = handler.lambda_handler(
            {
                "order_id": "ORD-1024", "category": "refund",
                "summary": "x", "severity": "catastrophic",
            },
            gateway_context("create_ticket"),
        )
        assert "severity" in result["error"]

    def test_summary_is_truncated_not_rejected(self, tools):
        handler, fake = tools
        seed_order(fake)
        result = handler.lambda_handler(
            {"order_id": "ORD-1024", "category": "other", "summary": "长" * 5000},
            gateway_context("create_ticket"),
        )
        stored = fake.items[(f"TICKET#{result['ticket_id']}", "META")]
        assert len(stored["summary"]["S"]) == handler._MAX_SUMMARY_LEN

    def test_two_tickets_get_distinct_ids(self, tools):
        handler, fake = tools
        seed_order(fake)
        args = {"order_id": "ORD-1024", "category": "other", "summary": "x"}
        first = handler.lambda_handler(dict(args), gateway_context("create_ticket"))
        second = handler.lambda_handler(dict(args), gateway_context("create_ticket"))
        assert first["ticket_id"] != second["ticket_id"]


# ---------------------------------------------------------------------------
# list_tickets / get_refund_policy
# ---------------------------------------------------------------------------


class TestListTickets:
    def test_lists_tickets_for_order_only(self, tools):
        handler, fake = tools
        seed_order(fake, order_id="ORD-1024")
        seed_order(fake, order_id="ORD-2048")
        for order_id in ("ORD-1024", "ORD-1024", "ORD-2048"):
            handler.lambda_handler(
                {"order_id": order_id, "category": "other", "summary": "x"},
                gateway_context("create_ticket"),
            )

        result = handler.lambda_handler(
            {"order_id": "ORD-1024"}, gateway_context("list_tickets")
        )
        assert result["count"] == 2
        assert all(t["order_id"] == "ORD-1024" for t in result["tickets"])

    def test_empty_is_not_an_error(self, tools):
        handler, fake = tools
        seed_order(fake)
        result = handler.lambda_handler(
            {"order_id": "ORD-1024"}, gateway_context("list_tickets")
        )
        assert result == {"order_id": "ORD-1024", "count": 0, "tickets": []}


class TestRefundPolicy:
    def test_returns_all_rules_by_default(self, tools):
        handler, _ = tools
        result = handler.lambda_handler({}, gateway_context("get_refund_policy"))
        assert result["currency"] == "CNY"
        assert len(result["rules"]) >= 3

    def test_filters_by_category(self, tools):
        handler, _ = tools
        result = handler.lambda_handler(
            {"category": "logistics_delay"}, gateway_context("get_refund_policy")
        )
        assert len(result["rules"]) == 1
        assert result["rules"][0]["rate_per_period"] == 0.05

    def test_unknown_category_lists_valid_ones(self, tools):
        handler, _ = tools
        result = handler.lambda_handler(
            {"category": "nope"}, gateway_context("get_refund_policy")
        )
        assert "logistics_delay" in result["error"]

    def test_policy_tells_model_to_use_cents(self, tools):
        """规则里必须明确要求按分计算,否则模型会用浮点算出 0.30000000000000004。"""
        handler, _ = tools
        result = handler.lambda_handler({}, gateway_context("get_refund_policy"))
        assert "分" in result["note"]


# ---------------------------------------------------------------------------
# 错误处理
# ---------------------------------------------------------------------------


class TestErrorHandling:
    def test_internal_errors_do_not_leak_details(self, tools, monkeypatch):
        handler, _ = tools

        def boom(event):
            raise RuntimeError("内部细节:表名是 secret-table,连接串 xyz")

        monkeypatch.setitem(handler.TOOLS, "get_order", boom)
        result = handler.lambda_handler(
            {"order_id": "ORD-1024"}, gateway_context("get_order")
        )

        assert "secret-table" not in str(result)
        assert result["error"] == "工具内部错误,请联系管理员查看日志"

    def test_dynamodb_failure_is_reported_generically(self, tools, monkeypatch):
        handler, _ = tools
        from botocore.exceptions import ClientError

        def boom(**kwargs):
            raise ClientError(
                {"Error": {"Code": "ProvisionedThroughputExceededException",
                           "Message": "slow down"}},
                "GetItem",
            )

        monkeypatch.setattr(handler._ddb, "get_item", boom)
        result = handler.lambda_handler(
            {"order_id": "ORD-1024"}, gateway_context("get_order")
        )
        assert "ProvisionedThroughputExceededException" in result["error"]
        assert "重试" in result["error"]

    def test_non_dict_event_is_rejected(self, tools):
        handler, _ = tools
        result = handler.lambda_handler(["not", "a", "dict"], gateway_context("get_order"))
        assert "error" in result


# ---------------------------------------------------------------------------
# schema 与实现不漂移
# ---------------------------------------------------------------------------


class TestSchemaConsistency:
    def test_every_schema_tool_has_an_implementation(self, tools):
        handler, _ = tools
        schema_names = {t["name"] for t in handler.TOOL_SCHEMA}
        assert schema_names == set(handler.TOOLS), (
            f"schema 与实现不一致:只在 schema 里 {schema_names - set(handler.TOOLS)},"
            f"只在实现里 {set(handler.TOOLS) - schema_names}"
        )

    def test_schema_entries_are_well_formed(self, tools):
        handler, _ = tools
        for tool in handler.TOOL_SCHEMA:
            assert tool["name"] and tool["description"], tool
            schema = tool["inputSchema"]
            assert schema["type"] == "object"
            for required in schema.get("required", []):
                assert required in schema["properties"], (
                    f"{tool['name']} 的 required 里有 {required},但 properties 里没有"
                )

    def test_descriptions_are_substantial(self, tools):
        """中国区 Gateway 没有语义检索,模型完全靠 description 选工具,
        描述太短等于让它瞎猜。"""
        handler, _ = tools
        for tool in handler.TOOL_SCHEMA:
            assert len(tool["description"]) >= 30, f"{tool['name']} 的描述太短"

    def test_write_tool_is_marked_as_such(self, tools):
        """写操作必须在描述里讲清楚,否则模型会随手调用。"""
        handler, _ = tools
        create = next(t for t in handler.TOOL_SCHEMA if t["name"] == "create_ticket")
        assert "写操作" in create["description"]
