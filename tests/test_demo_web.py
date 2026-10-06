"""本地 AgentCore Web 演示台的安全与协议测试。"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import demo_web  # noqa: E402


@pytest.fixture
def config() -> demo_web.AppConfig:
    return demo_web.AppConfig(
        profile="test-profile",
        region="cn-northwest-1",
        project="agentcore-cn",
        runtime_arn=(
            "arn:aws-cn:bedrock-agentcore:cn-northwest-1:111122223333:"
            "runtime/agentcore_cn_agent-test"
        ),
        username="demo-user",
        password="not-for-production",
        client_id="demo-client",
        client_secret="super-secret",
        token_endpoint="https://idp.example.test/oauth2/token",
        default_qualifier="stable",
    )


class FakeStream:
    def __init__(self, body: bytes):
        self.body = body

    def read(self) -> bytes:
        return self.body

    def iter_chunks(self, chunk_size: int = 64):
        for pos in range(0, len(self.body), chunk_size):
            yield self.body[pos : pos + chunk_size]


class FakeService:
    def __init__(self, config: demo_web.AppConfig):
        self.config = config
        self.invocations: list[tuple[dict, str, str]] = []

    def invoke(self, payload, *, session_id, qualifier):
        self.invocations.append((payload, session_id, qualifier))
        body = (
            'data: {"type":"tool","name":"business___get_order"}\n\n'
            'data: {"type":"text","delta":"订单已查询"}\n\n'
            f'data: {{"type":"done","sessionId":"{session_id}"}}\n\n'
        ).encode()
        return {"contentType": "text/event-stream", "response": FakeStream(body)}

    def run_selftest(self, *, qualifier):
        return {
            "summary": {"ok": 1, "total": 1, "failed": 0, "skipped": 0},
            "steps": [
                {
                    "name": "容器与入口",
                    "component": "Runtime",
                    "status": "ok",
                    "duration_ms": 1,
                    "detail": qualifier,
                    "error": "",
                }
            ],
        }

    def list_resources(self, resource_type):
        if resource_type != "orders":
            return {"resource": resource_type, "label": "资源", "count": 0, "items": []}
        return {
            "resource": "orders",
            "label": "订单",
            "count": 1,
            "items": [{"order_id": "ORD-1024", "amount_cents": 129900}],
            "refreshedAt": 1,
            "source": "DynamoDB · agentcore-cn-business（只读）",
        }


@pytest.fixture
def server(config):
    service = FakeService(config)
    httpd = demo_web.DemoServer(("127.0.0.1", 0), demo_web.DemoHandler, service)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd, service, f"http://127.0.0.1:{httpd.server_port}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


LOCAL_OPENER = build_opener(ProxyHandler({}))


def post_json(url: str, payload: dict, *, marker: bool = True):
    headers = {"Content-Type": "application/json"}
    if marker:
        headers["X-Demo-Request"] = "1"
    return LOCAL_OPENER.open(
        Request(url, data=json.dumps(payload).encode(), headers=headers, method="POST"),
        timeout=3,
    )


class TestDotenv:
    def test_loads_values_without_executing_shell(self, tmp_path, monkeypatch):
        marker = tmp_path / "must-not-exist"
        env_file = tmp_path / ".env"
        env_file.write_text(
            "PLAIN=value\n"
            "QUOTED='hello world'\n"
            "export EXPORTED=yes\n"
            f"UNTRUSTED=$(touch {marker})\n"
            "BAD-KEY=ignored\n",
            encoding="utf-8",
        )
        for key in ("PLAIN", "QUOTED", "EXPORTED", "UNTRUSTED"):
            monkeypatch.delenv(key, raising=False)

        demo_web.load_dotenv(env_file)

        assert os.environ["PLAIN"] == "value"
        assert os.environ["QUOTED"] == "hello world"
        assert os.environ["EXPORTED"] == "yes"
        assert os.environ["UNTRUSTED"].startswith("$(touch ")
        assert not marker.exists()
        assert "BAD-KEY" not in os.environ

    def test_does_not_override_existing_values(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text("KEEP=from-file\n", encoding="utf-8")
        monkeypatch.setenv("KEEP", "from-process")
        demo_web.load_dotenv(env_file)
        assert os.environ["KEEP"] == "from-process"


class TestConfigAndValidation:
    def test_public_config_never_exposes_secrets(self, config):
        raw = json.dumps(config.public_dict())
        assert config.password not in raw
        assert config.client_secret not in raw
        assert config.token_endpoint not in raw
        assert config.profile not in raw
        assert config.runtime_id in raw

    def test_new_session_meets_runtime_contract(self):
        session_id = demo_web.new_session_id()
        assert len(session_id) >= 33
        assert demo_web.SESSION_RE.fullmatch(session_id)

    @pytest.mark.parametrize("value", ["", None])
    def test_empty_session_creates_a_new_one(self, value):
        assert demo_web.SESSION_RE.fullmatch(demo_web.validate_session_id(value))

    @pytest.mark.parametrize("value", ["short", "x" * 101, "x" * 32 + "/"])
    def test_invalid_session_is_rejected(self, value):
        with pytest.raises(demo_web.DemoError):
            demo_web.validate_session_id(value)

    def test_prompt_is_bounded(self):
        assert demo_web.validate_prompt("  hello  ") == "hello"
        with pytest.raises(demo_web.DemoError):
            demo_web.validate_prompt("x" * (demo_web.MAX_PROMPT_CHARS + 1))

    def test_qualifier_is_restricted(self):
        assert demo_web.validate_qualifier("stable", "DEFAULT") == "stable"
        with pytest.raises(demo_web.DemoError):
            demo_web.validate_qualifier("../../secret", "DEFAULT")


class TestHttpSurface:
    def test_home_has_security_headers_and_no_external_assets(self, server):
        _, _, base = server
        with LOCAL_OPENER.open(base + "/", timeout=3) as response:
            body = response.read().decode()
            assert response.headers["X-Frame-Options"] == "DENY"
            assert "default-src 'self'" in response.headers["Content-Security-Policy"]
        assert "AgentCore 智能售后演示台" in body
        assert "https://" not in body
        assert "<script src=" not in body

    def test_public_config_contains_no_credentials(self, server, config):
        _, _, base = server
        with LOCAL_OPENER.open(base + "/api/config", timeout=3) as response:
            body = response.read().decode()
        assert config.password not in body
        assert config.client_secret not in body
        assert config.profile not in body

    def test_post_requires_custom_header_to_block_cross_site_forms(self, server):
        _, _, base = server
        with pytest.raises(HTTPError) as caught:
            post_json(base + "/api/selftest", {}, marker=False)
        assert caught.value.code == 400
        payload = json.loads(caught.value.read())
        assert "请求标记" in payload["error"]

    def test_chat_proxies_sse_and_generates_session_server_side(self, server):
        _, service, base = server
        with post_json(
            base + "/api/chat",
            {"prompt": "查询 ORD-1024", "qualifier": "stable"},
        ) as response:
            body = response.read().decode()
            assert response.headers["Content-Type"].startswith("text/event-stream")
        assert '"type": "meta"' in body
        assert "business___get_order" in body
        assert "订单已查询" in body
        payload, session_id, qualifier = service.invocations[0]
        assert payload == {"mode": "stream", "prompt": "查询 ORD-1024"}
        assert demo_web.SESSION_RE.fullmatch(session_id)
        assert qualifier == "stable"

    def test_selftest_is_returned_as_structured_json(self, server):
        _, _, base = server
        with post_json(base + "/api/selftest", {"qualifier": "stable"}) as response:
            payload = json.load(response)
        assert payload["summary"]["ok"] == 1
        assert payload["steps"][0]["component"] == "Runtime"

    def test_resources_are_returned_as_read_only_json(self, server):
        _, _, base = server
        with post_json(base + "/api/resources", {"resource": "orders"}) as response:
            payload = json.load(response)
        assert payload["resource"] == "orders"
        assert payload["count"] == 1
        assert payload["items"][0]["order_id"] == "ORD-1024"


class TestResourceProjection:
    class FakeDynamoDB:
        def __init__(self, items):
            self.items = items
            self.calls = []

        def scan(self, **kwargs):
            self.calls.append(kwargs)
            return {"Items": self.items}

    class FakeSession:
        def __init__(self, client):
            self.client_value = client

        def client(self, service, **kwargs):
            assert service == "dynamodb"
            return self.client_value

    def service_with_items(self, config, items):
        service = object.__new__(demo_web.DemoService)
        service.config = config
        service._session = self.FakeSession(self.FakeDynamoDB(items))
        return service

    def test_orders_expose_only_explicit_business_fields(self, config):
        service = self.service_with_items(
            config,
            [{
                "PK": {"S": "ORDER#ORD-1024"},
                "SK": {"S": "META"},
                "GSI1PK": {"S": "CUSTOMER#CUST-001"},
                "order_id": {"S": "ORD-1024"},
                "amount_cents": {"N": "129900"},
                "internal_note": {"S": "must-not-leak"},
            }],
        )
        result = service.list_resources("ORDERS")
        assert result["resource"] == "orders"
        assert result["items"] == [{"order_id": "ORD-1024", "amount_cents": 129900}]
        assert "must-not-leak" not in json.dumps(result)
        assert result["source"].endswith("business（只读）")

    def test_shipment_events_have_a_nested_allowlist(self, config):
        service = self.service_with_items(
            config,
            [{
                "PK": {"S": "SHIPMENT#SF1"},
                "SK": {"S": "META"},
                "shipment_no": {"S": "SF1"},
                "events": {"L": [{"M": {
                    "at": {"N": "123"},
                    "location": {"S": "西安"},
                    "note": {"S": "滞留"},
                    "internal_code": {"S": "secret"},
                }}]},
            }],
        )
        event = service.list_resources("shipments")["items"][0]["events"][0]
        assert event == {"at": 123, "location": "西安", "note": "滞留"}

    def test_auth_and_memory_resources_are_rejected(self, config):
        service = self.service_with_items(config, [])
        for resource in ("auth", "memory", "users", ""):
            with pytest.raises(demo_web.DemoError):
                service.list_resources(resource)


def test_html_uses_text_content_for_runtime_data():
    source = (REPO_ROOT / "web" / "demo.html").read_text(encoding="utf-8")
    assert "textContent" in source
    assert ".innerHTML" not in source
    assert "X-Demo-Request" in source
    assert re.search(r"window\.confirm\(.+MemoryLite", source)
    assert source.index('data-mode="intro"') < source.index('data-mode="oneStop"')
    assert 'class="mode-panel active" id="introPanel"' in source
    assert source.count('class="pain-item"') == 4
    assert source.count('class="journey-step"') == 6
    assert 'data-go-mode="oneStop"' in source
    assert '["intro", "oneStop", "guided", "resources"]' in source
