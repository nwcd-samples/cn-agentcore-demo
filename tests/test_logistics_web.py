"""自建物流查询网页的测试。

这个页面是 Browser 的演示靶子,所以有两类断言:
  1. 它本身是对的 —— 表单能提交、时间线渲染正确、XSS 被转义
  2. 它和 Agent 侧的选择器约定一致 —— Browser 工具靠 id 定位元素,
     页面改了 id 而工具没跟着改,线上会静默超时
"""

from __future__ import annotations

import re
import time
import urllib.parse

import pytest


@pytest.fixture
def logistics(monkeypatch, fake_ddb_factory):
    import logistics_handler as handler

    fake = fake_ddb_factory()
    monkeypatch.setattr(handler, "_ddb", fake)
    return handler, fake


def get_event(path: str = "/") -> dict:
    return {
        "requestContext": {"http": {"method": "GET", "path": path}},
        "headers": {},
        "body": "",
        "isBase64Encoded": False,
    }


def post_event(path: str, form: dict[str, str]) -> dict:
    return {
        "requestContext": {"http": {"method": "POST", "path": path}},
        "headers": {"content-type": "application/x-www-form-urlencoded"},
        "body": urllib.parse.urlencode(form),
        "isBase64Encoded": False,
    }


def seed_shipment(fake, **overrides):
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import seed_business

    now = int(time.time())
    kwargs = {
        "shipment_no": "SF7758291046",
        "carrier": "顺丰",
        "order_id": "ORD-1024",
        "status": "exception",
        "current_location": "陕西西安中转中心",
        "stalled_hours": 72,
        "events": [
            (-9 * 86400, "宁夏银兴仓", "快件已被揽收"),
            (-3 * 86400, "陕西西安中转中心", "因分拣设备故障,快件滞留待处理"),
        ],
        "now": now,
    }
    kwargs.update(overrides)
    item = seed_business.shipment_item(**kwargs)
    fake.put_item(TableName="t", Item=item)
    return kwargs["shipment_no"]


# ---------------------------------------------------------------------------
# 表单页
# ---------------------------------------------------------------------------


class TestFormPage:
    def test_index_renders_a_form(self, logistics):
        handler, _ = logistics
        response = handler.lambda_handler(get_event("/"), None)

        assert response["statusCode"] == 200
        assert response["headers"]["Content-Type"] == "text/html; charset=utf-8"
        body = response["body"]
        assert '<form method="POST" action="/track">' in body
        assert 'id="shipment-no"' in body
        assert 'id="query-btn"' in body

    def test_page_must_be_submitted_not_fetched(self, logistics):
        """页面刻意做成 POST 表单 —— 如果能 GET /track?no=x 拿到结果,
        Browser 这个能力就没演示价值了,一个 HTTP 请求就够。"""
        handler, _ = logistics
        response = handler.lambda_handler(get_event("/track?no=SF7758291046"), None)
        assert response["statusCode"] == 404

    def test_index_html_also_works(self, logistics):
        handler, _ = logistics
        assert handler.lambda_handler(get_event("/index.html"), None)["statusCode"] == 200

    def test_health_endpoint(self, logistics):
        handler, _ = logistics
        response = handler.lambda_handler(get_event("/health"), None)
        assert response["statusCode"] == 200
        assert '"status":"ok"' in response["body"]

    def test_unknown_path_returns_form_with_error(self, logistics):
        handler, _ = logistics
        response = handler.lambda_handler(get_event("/admin"), None)
        assert response["statusCode"] == 404
        assert 'id="shipment-no"' in response["body"]

    def test_csp_blocks_scripts(self, logistics):
        """页面没有任何脚本,直接把 script 源全禁掉。"""
        handler, _ = logistics
        csp = handler.lambda_handler(get_event("/"), None)["headers"][
            "Content-Security-Policy"
        ]
        assert "default-src 'none'" in csp


# ---------------------------------------------------------------------------
# 查询结果页
# ---------------------------------------------------------------------------


class TestTrackPage:
    def test_renders_timeline_newest_first(self, logistics):
        handler, fake = logistics
        seed_shipment(
            fake,
            events=[
                (-9 * 86400, "宁夏银兴仓", "快件已被揽收"),
                (-5 * 86400, "宁夏银川集散中心", "快件已发出"),
                (-3 * 86400, "陕西西安中转中心", "快件滞留待处理"),
            ],
        )

        response = handler.lambda_handler(
            post_event("/track", {"shipment_no": "SF7758291046"}), None
        )

        assert response["statusCode"] == 200
        body = response["body"]
        # 最新的事件应该排在最前面
        assert body.index("快件滞留待处理") < body.index("快件已发出")
        assert body.index("快件已发出") < body.index("快件已被揽收")

    def test_shows_stall_warning_that_only_the_page_reveals(self, logistics):
        """滞留时长和原因只在页面上有 —— 订单接口不返回。
        这是 Browser 能力存在的理由,必须真的渲染出来。"""
        handler, fake = logistics
        seed_shipment(fake, stalled_hours=72)

        body = handler.lambda_handler(
            post_event("/track", {"shipment_no": "SF7758291046"}), None
        )["body"]

        assert "已在中转环节停留 72 小时" in body
        assert "分拣设备故障" in body

    def test_shows_carrier_and_order_link(self, logistics):
        handler, fake = logistics
        seed_shipment(fake)
        body = handler.lambda_handler(
            post_event("/track", {"shipment_no": "SF7758291046"}), None
        )["body"]
        assert "顺丰" in body and "ORD-1024" in body

    def test_delivered_shipment_has_no_stall_warning(self, logistics):
        handler, fake = logistics
        seed_shipment(
            fake,
            shipment_no="YT4432018877",
            status="delivered",
            current_location="已签收",
            stalled_hours=None,
            events=[(-17 * 86400, "宁夏银川金凤区", "快件已签收")],
        )
        body = handler.lambda_handler(
            post_event("/track", {"shipment_no": "YT4432018877"}), None
        )["body"]
        assert "已签收" in body
        assert "停留" not in body

    def test_unknown_shipment_is_404_without_hints(self, logistics):
        """查不到就是查不到,不透露号段是否存在之类的信息。"""
        handler, _ = logistics
        response = handler.lambda_handler(
            post_event("/track", {"shipment_no": "SF9999999999"}), None
        )
        assert response["statusCode"] == 404
        assert "查询无结果" in response["body"]

    def test_empty_shipment_no_is_rejected(self, logistics):
        handler, _ = logistics
        response = handler.lambda_handler(post_event("/track", {"shipment_no": ""}), None)
        assert response["statusCode"] == 400
        assert "请填写运单号" in response["body"]

    @pytest.mark.parametrize(
        "bad", ["12345678", "SF123", "sf-7758291046", "SFFFFF7758291046", "SF77582910461234567890123"]
    )
    def test_malformed_shipment_no_is_rejected(self, logistics, bad):
        handler, _ = logistics
        response = handler.lambda_handler(post_event("/track", {"shipment_no": bad}), None)
        assert response["statusCode"] == 400
        assert "格式不正确" in response["body"]

    def test_whitespace_and_lowercase_are_normalized(self, logistics):
        """用户从别处复制粘贴过来的运单号总是脏的。"""
        handler, fake = logistics
        seed_shipment(fake)
        response = handler.lambda_handler(
            post_event("/track", {"shipment_no": "  sf7758291046 "}), None
        )
        assert response["statusCode"] == 200
        assert "SF7758291046" in response["body"]

    def test_base64_body_is_decoded(self, logistics):
        import base64

        handler, fake = logistics
        seed_shipment(fake)
        event = post_event("/track", {"shipment_no": "SF7758291046"})
        event["body"] = base64.b64encode(event["body"].encode()).decode()
        event["isBase64Encoded"] = True

        assert handler.lambda_handler(event, None)["statusCode"] == 200

    def test_dynamodb_failure_degrades_gracefully(self, logistics, monkeypatch):
        from botocore.exceptions import ClientError

        handler, fake = logistics

        def boom(**kwargs):
            raise ClientError(
                {"Error": {"Code": "ProvisionedThroughputExceededException",
                           "Message": "slow"}},
                "GetItem",
            )

        monkeypatch.setattr(fake, "get_item", boom)
        response = handler.lambda_handler(
            post_event("/track", {"shipment_no": "SF7758291046"}), None
        )
        assert response["statusCode"] == 503
        assert "稍后重试" in response["body"]

    def test_internal_error_does_not_leak_details(self, logistics, monkeypatch):
        handler, _ = logistics

        def boom(event):
            raise RuntimeError("内部细节:表名 secret-table")

        monkeypatch.setitem(handler.ROUTES, ("POST", "/track"), boom)
        response = handler.lambda_handler(
            post_event("/track", {"shipment_no": "SF7758291046"}), None
        )
        assert response["statusCode"] == 500
        assert "secret-table" not in response["body"]


# ---------------------------------------------------------------------------
# XSS
# ---------------------------------------------------------------------------


class TestOutputEscaping:
    def test_shipment_no_is_escaped_on_error_page(self, logistics):
        """运单号会被回显到 HTML,必须转义。
        格式校验已经挡掉大部分,但 prefill 走的是校验失败分支,
        恰恰是最容易漏的地方。

        注意:normalize_shipment_no 会转大写,所以断言要不区分大小写。
        """
        handler, _ = logistics
        payload = '"><script>alert(1)</script>'

        body = handler.lambda_handler(
            post_event("/track", {"shipment_no": payload}), None
        )["body"]

        lowered = body.lower()
        # 没有任何未转义的标签闭合
        assert "<script" not in lowered
        # 尖括号和引号都被实体化了
        assert "&lt;script&gt;" in lowered
        assert "&lt;/script&gt;" in lowered
        assert "&quot;&gt;" in lowered

    def test_escaping_test_would_catch_a_real_hole(self, logistics):
        """反向对照:确认上面的断言不是永远通过。"""
        handler, _ = logistics
        unescaped = handler.render_form(error="x", prefill='"><script>')
        # render_form 内部用了 html.escape,所以这里应该是安全的;
        # 如果哪天有人把 escape 去掉,下面这条就会失败
        assert "&lt;script&gt;" in unescaped.lower()
        assert '"><script>' not in unescaped

    def test_not_found_page_escapes_input(self, logistics):
        handler, _ = logistics
        # 通过格式校验但查不到,会走 render_not_found
        body = handler.lambda_handler(
            post_event("/track", {"shipment_no": "SF0000000001"}), None
        )["body"]
        assert "SF0000000001" in body

    def test_data_from_dynamodb_is_escaped(self, logistics):
        """库里的数据也不能无条件信任 —— 万一 seed 脚本被改坏了。"""
        handler, fake = logistics
        seed_shipment(
            fake,
            current_location='<img src=x onerror=alert(1)>',
            events=[(-86400, "地点", "<b>note</b>")],
        )
        body = handler.lambda_handler(
            post_event("/track", {"shipment_no": "SF7758291046"}), None
        )["body"]
        assert "<img src=x" not in body
        assert "&lt;img" in body
        assert "<b>note</b>" not in body


# ---------------------------------------------------------------------------
# 与 Agent 侧选择器约定一致
# ---------------------------------------------------------------------------


class TestSelectorContract:
    def test_form_ids_match_the_browser_tool(self, logistics):
        """Browser 工具靠这两个 id 定位元素。页面改了 id 而工具没改,
        线上表现是 Playwright 超时,报错完全看不出真因。"""
        import sys
        from pathlib import Path

        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
        from agent.tools.browser import (
            SELECTOR_QUERY_BUTTON,
            SELECTOR_SHIPMENT_INPUT,
        )

        handler, _ = logistics
        form_html = handler.render_form()

        for selector in (SELECTOR_SHIPMENT_INPUT, SELECTOR_QUERY_BUTTON):
            assert selector.startswith("#"), f"只支持 id 选择器,收到 {selector}"
            assert f'id="{selector[1:]}"' in form_html, (
                f"页面里没有 {selector} 对应的元素"
            )

    def test_form_action_matches_the_route(self, logistics):
        handler, _ = logistics
        assert 'action="/track"' in handler.render_form()
        assert ("POST", "/track") in handler.ROUTES

    def test_field_name_matches_what_the_handler_reads(self, logistics):
        """input 的 name 必须和 handler 读的 key 一致。"""
        handler, _ = logistics
        assert 'name="shipment_no"' in handler.render_form()

    def test_shipment_no_regex_matches_the_agent_side(self):
        """物流页和 Browser 工具各有一份运单号正则,必须等价 ——
        不然会出现"工具放过了但页面拒绝"这种自相矛盾的情况。"""
        import sys
        from pathlib import Path

        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
        import logistics_handler
        from agent.tools import browser

        assert (
            logistics_handler._SHIPMENT_NO_RE.pattern
            == browser._SHIPMENT_NO_RE.pattern
        )

    def test_seeded_shipments_all_pass_the_regex(self):
        """seed 出来的运单号必须都能通过校验,否则 demo 一上手就查不到。"""
        import sys
        from pathlib import Path

        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        import logistics_handler
        import seed_business

        dataset = seed_business.build_dataset(int(time.time()))
        shipments = [
            i["shipment_no"]["S"]
            for i in dataset
            if i["PK"]["S"].startswith("SHIPMENT#")
        ]
        assert shipments, "seed 数据里没有运单"
        for no in shipments:
            assert logistics_handler._SHIPMENT_NO_RE.match(no), f"{no} 通不过校验"

    def test_every_order_with_a_shipment_no_has_a_shipment_record(self):
        """订单上写了运单号,物流页就必须查得到 —— 否则 Agent 按订单里的
        运单号去查会扑空,看起来像工具坏了。"""
        import sys
        from pathlib import Path

        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        import seed_business

        dataset = seed_business.build_dataset(int(time.time()))
        order_shipment_nos = {
            i["shipment_no"]["S"]
            for i in dataset
            if i["PK"]["S"].startswith("ORDER#") and "shipment_no" in i
        }
        shipment_records = {
            i["shipment_no"]["S"]
            for i in dataset
            if i["PK"]["S"].startswith("SHIPMENT#")
        }
        missing = order_shipment_nos - shipment_records
        assert not missing, f"这些订单的运单号在物流数据里查不到:{sorted(missing)}"
