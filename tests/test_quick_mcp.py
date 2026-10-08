"""Amazon Quick Service-to-Service MCP 客户端的离线测试。"""

from __future__ import annotations

import base64
import io
import json
import os
import sys
from pathlib import Path
from urllib.parse import parse_qs

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import naming  # noqa: E402
import seed_auth  # noqa: E402
import verify_quick_mcp  # noqa: E402


@pytest.fixture(autouse=True)
def clean_quick_env(monkeypatch):
    for name in (
        "DEMO_CLIENT_ID",
        "GATEWAY_CLIENT_ID",
        "QUICK_CLIENT_ID",
        "QUICK_CLIENT_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)


class RecordingDynamoDB:
    def __init__(self):
        self.items = []

    def put_item(self, **kwargs):
        self.items.append(kwargs)
        return {}


class TestQuickClientSeed:
    def test_quick_client_is_independent_and_uses_client_credentials(self, monkeypatch):
        monkeypatch.setenv("QUICK_CLIENT_ID", "team-quick")
        monkeypatch.setenv("QUICK_CLIENT_SECRET", "quick-canary-secret")
        ddb = RecordingDynamoDB()

        client_id, secret, generated = seed_auth._put_quick_client(
            ddb, "auth-table", "agentcore-cn"
        )

        assert (client_id, secret, generated) == (
            "team-quick",
            "quick-canary-secret",
            False,
        )
        call = ddb.items[0]
        assert call["TableName"] == "auth-table"
        item = call["Item"]
        assert item["PK"]["S"] == "CLIENT#team-quick"
        assert item["grant_types"]["SS"] == ["client_credentials"]
        assert item["scopes"]["SS"] == naming.QUICK_CLIENT_SCOPES
        assert item["audience"]["S"] == "agentcore-cn"
        assert "quick-canary-secret" not in json.dumps(item)

    def test_client_ids_must_not_collide(self, monkeypatch):
        monkeypatch.setenv("QUICK_CLIENT_ID", "agentcore-cn-client")
        with pytest.raises(ValueError, match="互不相同"):
            naming.all_client_ids("agentcore-cn")

    def test_quick_scope_can_list_and_write_tools(self):
        assert set(naming.QUICK_CLIENT_SCOPES) == {
            "gateway:invoke",
            "tools:read",
            "tools:write",
        }


class TestQuickConfiguration:
    def test_discovers_token_endpoint_from_gateway_metadata(self, monkeypatch):
        calls = []

        def fake_get(url):
            calls.append(url)
            if url.endswith("oauth-protected-resource"):
                return {"authorization_servers": ["https://idp.example"]}
            return {"token_endpoint": "https://idp.example/oauth2/token"}

        monkeypatch.setattr(verify_quick_mcp, "_get_json", fake_get)
        endpoint = verify_quick_mcp.discover_token_endpoint(
            "https://gateway.example/mcp"
        )
        assert endpoint == "https://idp.example/oauth2/token"
        assert calls == [
            "https://gateway.example/.well-known/oauth-protected-resource",
            "https://idp.example/.well-known/oauth-authorization-server",
        ]

    def test_token_request_uses_quick_basic_auth_and_scopes(self, monkeypatch):
        recorded = {}

        def fake_urlopen(request, timeout):
            recorded["request"] = request
            recorded["timeout"] = timeout
            return io.BytesIO(b'{"access_token":"short-lived-token"}')

        monkeypatch.setattr(verify_quick_mcp, "urlopen", fake_urlopen)
        token = verify_quick_mcp.request_token(
            "https://idp.example/oauth2/token",
            client_id="agentcore-cn-quick",
            client_secret="canary-secret",
        )
        assert token == "short-lived-token"
        request = recorded["request"]
        auth = request.headers["Authorization"].removeprefix("Basic ")
        assert base64.b64decode(auth).decode() == "agentcore-cn-quick:canary-secret"
        form = parse_qs(request.data.decode())
        assert form["grant_type"] == ["client_credentials"]
        assert set(form["scope"][0].split()) == set(naming.QUICK_CLIENT_SCOPES)

    def test_script_never_prints_token_or_secret(self):
        source = (REPO_ROOT / "scripts" / "verify_quick_mcp.py").read_text()
        assert "print(token" not in source
        assert "print(client_secret" not in source
        assert '"access_token": token' not in source


class TestDeploymentWiring:
    def test_quick_deploy_mode_only_seeds_quick_then_updates_gateway(self):
        source = (REPO_ROOT / "scripts" / "deploy.sh").read_text()
        assert "scripts/seed_auth.py --quick-only" in source
        assert 'preflight; quick ;;' in source
        quick_body = source.split("quick() {", 1)[1].split("\n}", 1)[0]
        assert "scripts/seed_business.py" not in quick_body
        assert "scripts/create_gateway.py" in quick_body

    def test_gateway_allowlist_comes_from_shared_three_client_helper(self):
        import create_gateway

        source = (REPO_ROOT / "scripts" / "create_gateway.py").read_text()
        assert "naming.gateway_client_ids(args.project)" in source
        assert naming.gateway_client_ids("agentcore-cn") == [
            "agentcore-cn-client",
            "agentcore-cn-client-m2m",
            "agentcore-cn-quick",
        ]
        del create_gateway

    def test_local_proxy_prefers_quick_credentials_but_has_legacy_fallback(self):
        source = (REPO_ROOT / "scripts" / "quick_mcp_proxy.sh").read_text()
        quick_pos = source.index("QUICK_CLIENT_ID")
        gateway_pos = source.index("GATEWAY_CLIENT_ID")
        assert quick_pos < gateway_pos
        assert 'AUTH_CLIENT_ID="$QUICK_CLIENT_ID"' in source
        assert 'AUTH_CLIENT_ID="$GATEWAY_CLIENT_ID"' in source
        assert "mcp-remote@0.14.3" in source
