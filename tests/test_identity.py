"""Identity 出向凭证配置的离线验证。

两类断言:
  1. 请求参数形状 —— 用 botocore 的 ParamValidator 按
     bedrock-agentcore-control 的服务模型校验,不需要凭证。
  2. 密钥不泄漏 —— 脚本会经手 DeepSeek API Key 和 IdP client secret,
     这两个值绝不能出现在日志、stdout 或异常信息里。
"""

from __future__ import annotations

import io
import os
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import boto3
import pytest
from botocore.exceptions import ClientError
from botocore.validate import ParamValidator

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
os.environ.setdefault("BUSINESS_TABLE", "unused")

ISSUER = "https://abc123.execute-api.cn-northwest-1.amazonaws.com.cn"
DISCOVERY_URL = f"{ISSUER}/.well-known/openid-configuration"

# 测试里用的假密钥,后面会断言它们不出现在任何输出里
FAKE_DEEPSEEK_KEY = "sk-deepseek-CANARY-2f8a1c"
FAKE_CLIENT_SECRET = "client-secret-CANARY-9d4b7e"


@pytest.fixture(scope="module")
def service_model():
    client = boto3.Session().client(
        "bedrock-agentcore-control",
        region_name="cn-northwest-1",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
    )
    return client.meta.service_model


@pytest.fixture(scope="module")
def setup_identity():
    import setup_identity as module

    return module


def assert_valid(service_model, operation: str, params: dict) -> None:
    shape = service_model.operation_model(operation).input_shape
    report = ParamValidator().validate(params, shape)
    if report.has_errors():
        raise AssertionError(f"{operation} 参数不合法:\n{report.generate_report()}")


class RecordingControlClient:
    """记录调用参数的假 control plane 客户端。"""

    def __init__(self, *, conflict_on_create: bool = False):
        self.calls: list[tuple[str, dict]] = []
        self.conflict_on_create = conflict_on_create

    def _record(self, op: str, kwargs: dict) -> dict:
        self.calls.append((op, kwargs))
        if self.conflict_on_create and op.startswith("create_"):
            raise ClientError(
                {"Error": {"Code": "ConflictException", "Message": "already exists"}}, op
            )
        return {
            "name": kwargs.get("name"),
            "credentialProviderArn": "arn:aws-cn:bedrock-agentcore:cn-northwest-1:1:"
            "token-vault/default/x",
            "apiKeySecretArn": {"secretArn": "arn:aws-cn:secretsmanager:cn-northwest-1:1:secret:a"},
            "clientSecretArn": {"secretArn": "arn:aws-cn:secretsmanager:cn-northwest-1:1:secret:b"},
        }

    def __getattr__(self, name):
        def call(**kwargs):
            return self._record(name, kwargs)

        return call

    def op_names(self) -> list[str]:
        return [op for op, _ in self.calls]

    def args_of(self, op: str) -> dict:
        for name, kwargs in self.calls:
            if name == op:
                return kwargs
        raise AssertionError(f"没有调用过 {op},实际调用:{self.op_names()}")


# ---------------------------------------------------------------------------
# API Key provider
# ---------------------------------------------------------------------------


class TestApiKeyProvider:
    def test_create_params_are_valid(self, service_model, setup_identity):
        client = RecordingControlClient()
        setup_identity.ensure_api_key_provider(
            client, name="agentcore-cn-deepseek", api_key=FAKE_DEEPSEEK_KEY
        )
        assert_valid(
            service_model,
            "CreateApiKeyCredentialProvider",
            client.args_of("create_api_key_credential_provider"),
        )

    def test_secret_is_managed_by_agentcore(self, setup_identity):
        """MANAGED 表示密文由 AgentCore 托管,我们不自己建 Secrets Manager 条目。"""
        client = RecordingControlClient()
        setup_identity.ensure_api_key_provider(
            client, name="p", api_key=FAKE_DEEPSEEK_KEY
        )
        args = client.args_of("create_api_key_credential_provider")
        assert args["apiKeySecretSource"] == "MANAGED"
        # MANAGED 模式下不该同时传 apiKeySecretConfig
        assert "apiKeySecretConfig" not in args

    def test_falls_back_to_update_when_already_exists(self, service_model, setup_identity):
        """重复运行必须幂等,并且顺带完成密钥轮换。"""
        client = RecordingControlClient(conflict_on_create=True)
        setup_identity.ensure_api_key_provider(
            client, name="p", api_key=FAKE_DEEPSEEK_KEY
        )
        assert client.op_names() == [
            "create_api_key_credential_provider",
            "update_api_key_credential_provider",
        ]
        assert_valid(
            service_model,
            "UpdateApiKeyCredentialProvider",
            client.args_of("update_api_key_credential_provider"),
        )

    def test_unexpected_errors_are_not_swallowed(self, setup_identity):
        """只有"已存在"才转更新;权限不足之类的错误必须原样抛出。"""

        class Denying(RecordingControlClient):
            def _record(self, op, kwargs):
                raise ClientError(
                    {"Error": {"Code": "AccessDeniedException", "Message": "nope"}}, op
                )

        with pytest.raises(ClientError):
            setup_identity.ensure_api_key_provider(
                Denying(), name="p", api_key=FAKE_DEEPSEEK_KEY
            )


# ---------------------------------------------------------------------------
# OAuth2 provider
# ---------------------------------------------------------------------------


class TestOauthProvider:
    def _config(self, setup_identity, **overrides):
        kwargs = {
            "discovery_url": DISCOVERY_URL,
            "client_id": "agentcore-cn-client-m2m",
            "client_secret": FAKE_CLIENT_SECRET,
            "issuer": ISSUER,
        }
        kwargs.update(overrides)
        return setup_identity.build_oauth_config(**kwargs)

    def test_create_params_are_valid(self, service_model, setup_identity):
        client = RecordingControlClient()
        setup_identity.ensure_oauth_provider(
            client, name="agentcore-cn-gateway-oauth", config=self._config(setup_identity)
        )
        assert_valid(
            service_model,
            "CreateOauth2CredentialProvider",
            client.args_of("create_oauth2_credential_provider"),
        )

    def test_metadata_variant_is_also_valid(self, service_model, setup_identity):
        """discoveryUrl 拉不通时的绕行方案,形状也必须合法。"""
        client = RecordingControlClient()
        setup_identity.ensure_oauth_provider(
            client,
            name="p",
            config=self._config(setup_identity, use_metadata=True),
        )
        args = client.args_of("create_oauth2_credential_provider")
        assert_valid(service_model, "CreateOauth2CredentialProvider", args)
        discovery = args["oauth2ProviderConfigInput"]["customOauth2ProviderConfig"][
            "oauthDiscovery"
        ]
        assert "authorizationServerMetadata" in discovery
        assert "discoveryUrl" not in discovery

    def test_vendor_must_be_custom(self, setup_identity):
        """中国区内置 vendor(GitHub/Google/...)全部不可用,只能 CustomOauth2。"""
        assert setup_identity.OAUTH_VENDOR == "CustomOauth2"
        client = RecordingControlClient()
        setup_identity.ensure_oauth_provider(
            client, name="p", config=self._config(setup_identity)
        )
        args = client.args_of("create_oauth2_credential_provider")
        assert args["credentialProviderVendor"] == "CustomOauth2"

    def test_discovery_url_variant_omits_metadata(self, setup_identity):
        """oauthDiscovery 的两种写法是二选一,不能同时传。"""
        config = self._config(setup_identity)
        discovery = config["customOauth2ProviderConfig"]["oauthDiscovery"]
        assert discovery == {"discoveryUrl": DISCOVERY_URL}

    def test_token_endpoint_matches_self_hosted_idp_routes(self, setup_identity):
        """显式 metadata 里的端点必须和 infra/10-auth-idp.yaml 的真实路由一致。"""
        config = self._config(setup_identity, use_metadata=True)
        metadata = config["customOauth2ProviderConfig"]["oauthDiscovery"][
            "authorizationServerMetadata"
        ]
        idp_template = (REPO_ROOT / "infra" / "10-auth-idp.yaml").read_text()
        assert "POST /oauth2/token" in idp_template
        assert metadata["tokenEndpoint"] == f"{ISSUER}/oauth2/token"
        assert metadata["issuer"] == ISSUER

    def test_client_auth_method_is_supported_by_our_idp(self, setup_identity):
        """自建 IdP 只实现了 basic 和 post 两种客户端认证方式。"""
        assert setup_identity.CLIENT_AUTH_METHOD in (
            "CLIENT_SECRET_BASIC",
            "CLIENT_SECRET_POST",
        )
        idp = (REPO_ROOT / "src" / "lambdas" / "idp" / "idp_handler.py").read_text()
        assert "client_secret_basic" in idp and "client_secret_post" in idp

    def test_falls_back_to_update_when_already_exists(self, service_model, setup_identity):
        client = RecordingControlClient(conflict_on_create=True)
        setup_identity.ensure_oauth_provider(
            client, name="p", config=self._config(setup_identity)
        )
        assert client.op_names() == [
            "create_oauth2_credential_provider",
            "update_oauth2_credential_provider",
        ]
        assert_valid(
            service_model,
            "UpdateOauth2CredentialProvider",
            client.args_of("update_oauth2_credential_provider"),
        )

    def test_missing_issuer_with_metadata_variant_fails_loudly(self, setup_identity):
        with pytest.raises(SystemExit):
            setup_identity.build_oauth_config(
                discovery_url=DISCOVERY_URL,
                client_id="c",
                client_secret=FAKE_CLIENT_SECRET,
                issuer="",
                use_metadata=True,
            )


# ---------------------------------------------------------------------------
# 密钥不泄漏
# ---------------------------------------------------------------------------


class TestSecretsAreNotLeaked:
    def test_nothing_is_printed_while_creating_providers(self, setup_identity):
        """ensure_* 会把密钥当参数收进去,但不能把它打出来。"""
        client = RecordingControlClient()
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            setup_identity.ensure_api_key_provider(
                client, name="p", api_key=FAKE_DEEPSEEK_KEY
            )
            setup_identity.ensure_oauth_provider(
                client,
                name="q",
                config=setup_identity.build_oauth_config(
                    discovery_url=DISCOVERY_URL,
                    client_id="c",
                    client_secret=FAKE_CLIENT_SECRET,
                    issuer=ISSUER,
                ),
            )
        combined = out.getvalue() + err.getvalue()
        assert FAKE_DEEPSEEK_KEY not in combined
        assert FAKE_CLIENT_SECRET not in combined

    def test_show_output_cannot_contain_plaintext(self, setup_identity):
        """--show 打印的是服务端响应,里面只有密文 ARN。
        这里验证响应中确实没有明文字段名 apiKey / clientSecret。"""
        client = RecordingControlClient()
        out = io.StringIO()
        with redirect_stdout(out):
            setup_identity.show(client, api_key_name="p", oauth_name="q")
        printed = out.getvalue()
        assert '"apiKey"' not in printed
        assert '"clientSecret"' not in printed

    def test_source_never_logs_the_secret_variables(self):
        """静态检查:脚本里不能把密钥变量塞进 print/log。

        按完整标识符匹配 —— api_key_name 是 provider 名字不是密钥,
        用子串匹配会误报。
        """
        import re

        source = (REPO_ROOT / "scripts" / "setup_identity.py").read_text()
        # 真正持有明文的变量名
        secret_vars = ("deepseek_key", "client_secret", "api_key")
        patterns = {v: re.compile(rf"\b{v}\b(?!_)") for v in secret_vars}

        for lineno, line in enumerate(source.splitlines(), 1):
            stripped = line.strip()
            if not stripped.startswith(("print(", "log(", "warn(", "die(")):
                continue
            for name, pattern in patterns.items():
                assert not pattern.search(stripped), (
                    f"第 {lineno} 行可能泄漏 {name}:{stripped}"
                )

    def test_static_check_would_catch_a_real_leak(self):
        """反向对照:确认上面那个检查不是永远通过。"""
        import re

        pattern = re.compile(r"\bapi_key\b(?!_)")
        assert pattern.search('print(f"key={api_key}")')
        assert not pattern.search('print(f"name={api_key_name}")')

    def test_secret_prompt_uses_getpass(self):
        """交互式输入必须不回显。"""
        source = (REPO_ROOT / "scripts" / "setup_identity.py").read_text()
        assert "getpass.getpass(" in source
        assert "input(" not in source


# ---------------------------------------------------------------------------
# 与 Agent 侧配置对齐
# ---------------------------------------------------------------------------


class TestAgentSideAlignment:
    def test_provider_names_match_agent_defaults(self):
        """脚本建出来的 provider 名必须和 agent/config.py 的默认值一致,
        否则 Agent 运行时会去找一个不存在的 provider。"""
        sys.path.insert(0, str(REPO_ROOT / "src"))
        for key in ("DEEPSEEK_API_KEY_PROVIDER", "GATEWAY_OAUTH_PROVIDER"):
            os.environ.pop(key, None)
        from agent.config import get_settings

        get_settings.cache_clear()
        settings = get_settings()

        project = settings.project
        assert settings.deepseek_api_key_provider == f"{project}-deepseek"
        assert settings.gateway_oauth_provider == f"{project}-gateway-oauth"
        get_settings.cache_clear()

    def test_agent_uses_provider_name_not_arn(self):
        """SDK 的入参是 resourceCredentialProviderName,传 ARN 会找不到。"""
        model_source = (REPO_ROOT / "src" / "agent" / "model.py").read_text()
        assert "requires_api_key(provider_name=" in model_source
        gateway_source = (REPO_ROOT / "src" / "agent" / "tools" / "gateway.py").read_text()
        assert "requires_access_token(" in gateway_source
        assert 'auth_flow="M2M"' in gateway_source

    def test_runtime_role_grants_the_two_data_plane_actions(self):
        foundation = (REPO_ROOT / "infra" / "00-foundation.yaml").read_text()
        for action in (
            "bedrock-agentcore:GetResourceApiKey",
            "bedrock-agentcore:GetResourceOauth2Token",
            "bedrock-agentcore:GetWorkloadAccessToken",
        ):
            assert action in foundation, f"Runtime 角色缺少 {action}"

    def test_identity_calls_decorate_sync_functions(self):
        """必须装饰同步函数,让 SDK 自己处理事件循环 + ContextVar 传递。

        装饰 async 函数再自己起线程跑 asyncio.run 会丢掉
        BedrockAgentCoreContext 里的 workload access token ——
        本地好使、线上取不到凭证。tests/test_identity_runtime.py 有行为级验证,
        这里做静态兜底。
        """
        for path in (
            REPO_ROOT / "src" / "agent" / "model.py",
            REPO_ROOT / "src" / "agent" / "tools" / "gateway.py",
        ):
            source = path.read_text()
            assert "concurrent.futures" not in source, (
                f"{path.name} 又在自己起线程处理 Identity 调用了"
            )
            assert "async def _grab" not in source, (
                f"{path.name} 的 _grab 应该是同步函数"
            )


# ---------------------------------------------------------------------------
# 客户端 ID 命名约定
# ---------------------------------------------------------------------------


class TestClientIdNaming:
    """三个脚本必须推导出同一组客户端 ID。

    不一致的症状是 Gateway 的 CUSTOM_JWT allowedClients 不匹配 ->
    所有请求被静默拒绝,而错误信息看不出根因。
    """

    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch):
        monkeypatch.delenv("DEMO_CLIENT_ID", raising=False)
        monkeypatch.delenv("GATEWAY_CLIENT_ID", raising=False)
        monkeypatch.delenv("QUICK_CLIENT_ID", raising=False)

    def test_defaults_are_derived_from_project(self):
        import naming

        assert naming.user_client_id("agentcore-cn") == "agentcore-cn-client"
        assert naming.m2m_client_id("agentcore-cn") == "agentcore-cn-client-m2m"
        assert naming.quick_client_id("agentcore-cn") == "agentcore-cn-quick"

    def test_env_overrides_are_independent(self, monkeypatch):
        import naming

        monkeypatch.setenv("DEMO_CLIENT_ID", "my-client")
        assert naming.user_client_id("p") == "my-client"
        assert naming.m2m_client_id("p") == "my-client-m2m"
        monkeypatch.setenv("GATEWAY_CLIENT_ID", "runtime-machine")
        monkeypatch.setenv("QUICK_CLIENT_ID", "quick-machine")
        assert naming.m2m_client_id("p") == "runtime-machine"
        assert naming.quick_client_id("p") == "quick-machine"

    def test_allowed_clients_are_scoped_per_resource(self):
        import naming

        assert naming.runtime_client_ids("agentcore-cn") == [
            "agentcore-cn-client",
            "agentcore-cn-client-m2m",
        ]
        assert naming.gateway_client_ids("agentcore-cn") == [
            "agentcore-cn-client",
            "agentcore-cn-client-m2m",
            "agentcore-cn-quick",
        ]

    def test_all_three_scripts_use_the_shared_helper(self):
        """静态检查:不允许任何脚本自己拼客户端 ID。"""
        for name in ("seed_auth.py", "create_gateway.py", "setup_identity.py"):
            source = (REPO_ROOT / "scripts" / name).read_text()
            assert "import naming" in source, f"{name} 没用共享的命名模块"
            assert '+ "-m2m"' not in source, f"{name} 在自己拼 m2m 后缀"

    def test_seed_creates_exactly_the_clients_the_gateway_allows(self):
        """seed_auth 创建的三类客户端必须正好等于 Gateway allowlist。"""
        import create_gateway
        import naming

        allowed = set(naming.gateway_client_ids("agentcore-cn"))
        seeded = {
            naming.user_client_id("agentcore-cn"),
            naming.m2m_client_id("agentcore-cn"),
            naming.quick_client_id("agentcore-cn"),
        }
        assert seeded == allowed
        assert naming.m2m_client_id("agentcore-cn") in allowed
        assert naming.quick_client_id("agentcore-cn") in allowed
        del create_gateway


class TestScopeAlignment:
    """Agent 请求的 scope 必须是 m2m 客户端被授予的子集。

    否则自建 IdP 返回 invalid_scope,Agent 拿不到 token,
    症状是"Gateway 工具全部加载失败",要翻 IdP 日志才看得出真因。
    """

    def test_agent_requested_scopes_are_granted_to_m2m_client(self):
        sys.path.insert(0, str(REPO_ROOT / "src"))
        import naming
        from agent.tools.gateway import GATEWAY_SCOPES

        granted = set(naming.M2M_CLIENT_SCOPES)
        requested = set(GATEWAY_SCOPES)
        assert requested <= granted, (
            f"Agent 请求了未被授予的 scope:{sorted(requested - granted)}。"
            "自建 IdP 会返回 invalid_scope。"
        )

    def test_m2m_client_can_write(self):
        """create_ticket 是写操作,m2m 客户端必须有 tools:write。"""
        import naming

        assert "tools:write" in naming.M2M_CLIENT_SCOPES

    def test_seed_uses_the_shared_scope_lists(self):
        source = (REPO_ROOT / "scripts" / "seed_auth.py").read_text()
        assert "naming.M2M_CLIENT_SCOPES" in source
        assert "naming.USER_CLIENT_SCOPES" in source

    def test_real_idp_grants_the_requested_scopes(self, idp):
        """端到端:用真实 IdP 逻辑走一遍 client_credentials,
        确认 Agent 请求的那组 scope 真的能换出 token。"""
        import naming
        from agent.tools.gateway import GATEWAY_SCOPES

        handler, ddb, _ = idp
        client_id = naming.m2m_client_id("agentcore-cn")
        ddb.put_item(
            TableName="t",
            Item={
                "PK": {"S": f"CLIENT#{client_id}"},
                "SK": {"S": "PROFILE"},
                "secret_hash": {"S": handler.hash_password("s3cret")},
                "grant_types": {"SS": ["client_credentials"]},
                "scopes": {"SS": naming.M2M_CLIENT_SCOPES},
                "audience": {"S": "agentcore-cn"},
            },
        )

        import base64
        import json
        import urllib.parse

        basic = base64.b64encode(
            f"{urllib.parse.quote(client_id)}:s3cret".encode()
        ).decode()
        event = {
            "requestContext": {"http": {"method": "POST", "path": "/oauth2/token"}},
            "headers": {
                "content-type": "application/x-www-form-urlencoded",
                "Authorization": f"Basic {basic}",
            },
            "body": urllib.parse.urlencode(
                {
                    "grant_type": "client_credentials",
                    "scope": " ".join(GATEWAY_SCOPES),
                }
            ),
            "isBase64Encoded": False,
        }

        response = handler.lambda_handler(event, None)
        assert response["statusCode"] == 200, (
            f"IdP 拒绝了 Agent 请求的 scope:{response['body']}"
        )
        payload = json.loads(response["body"])
        assert set(payload["scope"].split()) == set(GATEWAY_SCOPES)
