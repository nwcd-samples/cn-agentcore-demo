"""Identity 出向在 Runtime 环境下的行为测试。

这组测试针对一个具体的坑:workload access token 存在 ContextVar
(BedrockAgentCoreContext)里。如果在已有事件循环的情况下自己起线程跑
asyncio.run,ContextVar 不会传过去 —— 本地(没有事件循环)一切正常,
部署到 Runtime(entrypoint 是 async)就取不到凭证。

所以这里刻意在**运行中的事件循环里**调用,断言 token 真的传到了下游。
"""

from __future__ import annotations

import asyncio

import pytest


@pytest.fixture
def fake_identity(monkeypatch):
    """替换 IdentityClient,记录它收到的 workload access token。"""
    from bedrock_agentcore.identity import auth

    seen: dict[str, object] = {}

    class FakeIdentityClient:
        def __init__(self, region):
            seen["region"] = region

        async def get_api_key(self, *, provider_name, agent_identity_token):
            seen["api_key_provider"] = provider_name
            seen["api_key_wat"] = agent_identity_token
            return "sk-from-identity"

        async def get_token(self, *, provider_name, agent_identity_token, scopes, **kwargs):
            seen["oauth_provider"] = provider_name
            seen["oauth_wat"] = agent_identity_token
            seen["oauth_scopes"] = scopes
            seen["auth_flow"] = kwargs.get("auth_flow")
            return "access-token-from-identity"

    monkeypatch.setattr(auth, "IdentityClient", FakeIdentityClient)
    return seen


@pytest.fixture(autouse=True)
def clear_caches():
    import agent.model as model
    import agent.tools.gateway as gateway

    model._cached_api_key = None
    gateway._cached_token = None
    yield
    model._cached_api_key = None
    gateway._cached_token = None


def set_workload_token(token: str) -> None:
    from bedrock_agentcore.runtime import BedrockAgentCoreContext

    BedrockAgentCoreContext.set_workload_access_token(token)


class TestApiKeyFromIdentity:
    def test_works_outside_an_event_loop(self, fake_identity, monkeypatch):
        import agent.model as model

        set_workload_token("wat-local")
        assert model._fetch_api_key_from_identity("agentcore-cn-deepseek") == "sk-from-identity"
        assert fake_identity["api_key_provider"] == "agentcore-cn-deepseek"

    def test_context_var_survives_inside_a_running_loop(self, fake_identity):
        """这就是那个坑:Runtime 的 entrypoint 是 async 的,
        取凭证时已经在事件循环里。ContextVar 必须传到工作线程。

        已验证这个断言有效:把实现改回"自己起线程跑 asyncio.run"的写法,
        这个用例立刻失败。
        """
        import agent.model as model

        async def inside_loop() -> str:
            set_workload_token("wat-in-loop")
            # 在协程里同步调用,模拟 build_model() 的真实处境
            return model._fetch_api_key_from_identity("agentcore-cn-deepseek")

        result = asyncio.run(inside_loop())

        assert result == "sk-from-identity"
        assert fake_identity["api_key_wat"] == "wat-in-loop", (
            "workload access token 没有传到工作线程 —— "
            "说明又在自己起线程跑 asyncio.run,ContextVar 丢了"
        )

    def test_provider_name_is_passed_not_arn(self, fake_identity):
        """SDK 的入参是 resourceCredentialProviderName,传 ARN 会找不到。"""
        import agent.model as model

        set_workload_token("wat")
        model._fetch_api_key_from_identity("agentcore-cn-deepseek")
        assert not str(fake_identity["api_key_provider"]).startswith("arn:")


class TestGatewayTokenFromIdentity:
    def test_context_var_survives_inside_a_running_loop(self, fake_identity):
        import agent.tools.gateway as gateway

        async def inside_loop() -> str:
            set_workload_token("wat-gw")
            return gateway._token_from_identity(
                "agentcore-cn-gateway-oauth", gateway.GATEWAY_SCOPES
            )

        result = asyncio.run(inside_loop())

        assert result == "access-token-from-identity"
        assert fake_identity["oauth_wat"] == "wat-gw"

    def test_uses_m2m_flow_with_gateway_scopes(self, fake_identity):
        """Gateway 是机器到机器调用,不该走用户联邦流程。"""
        import agent.tools.gateway as gateway

        set_workload_token("wat")
        gateway._token_from_identity("p", gateway.GATEWAY_SCOPES)

        assert fake_identity["auth_flow"] == "M2M"
        assert fake_identity["oauth_scopes"] == gateway.GATEWAY_SCOPES


class TestLocalFallbacks:
    def test_env_var_short_circuits_identity(self, monkeypatch):
        """本地调试时用 DEEPSEEK_API_KEY,不该去打 Identity。"""
        import agent.model as model
        from agent.config import get_settings

        get_settings.cache_clear()
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-local-debug")

        def should_not_be_called(_provider):
            raise AssertionError("设了环境变量就不该去 Identity 取")

        monkeypatch.setattr(model, "_fetch_api_key_from_identity", should_not_be_called)

        assert model.get_api_key(get_settings()) == "sk-local-debug"
        get_settings.cache_clear()

    def test_api_key_is_cached(self, fake_identity, monkeypatch):
        import agent.model as model
        from agent.config import get_settings

        get_settings.cache_clear()
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        settings = get_settings()
        assert not settings.deepseek_api_key_env

        set_workload_token("wat")
        calls: list[str] = []
        original = model._fetch_api_key_from_identity

        def counting(provider):
            calls.append(provider)
            return original(provider)

        monkeypatch.setattr(model, "_fetch_api_key_from_identity", counting)

        model.get_api_key(settings)
        model.get_api_key(settings)
        assert len(calls) == 1, "第二次应该走缓存,不该再打 Identity"

        model.get_api_key(settings, refresh=True)
        assert len(calls) == 2, "refresh=True 必须强制重取"
        get_settings.cache_clear()

    def test_empty_key_from_identity_is_an_error(self, monkeypatch):
        """Identity 返回空值时要报清楚,而不是拿空 key 去打 DeepSeek 拿 401。"""
        import agent.model as model
        from agent.config import get_settings

        get_settings.cache_clear()
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        monkeypatch.setattr(model, "_fetch_api_key_from_identity", lambda _p: "")

        with pytest.raises(RuntimeError, match="setup_identity"):
            model.get_api_key(get_settings())
        get_settings.cache_clear()


class TestGatewayDirectIdpFallback:
    def test_direct_idp_path_is_used_when_configured(self, monkeypatch):
        """本地调试:配了 IDP_TOKEN_ENDPOINT 就直连自建 IdP,不走 Identity。"""
        import agent.tools.gateway as gateway
        from agent.config import get_settings

        get_settings.cache_clear()
        monkeypatch.setenv("IDP_TOKEN_ENDPOINT", "https://idp.test/oauth2/token")
        monkeypatch.setenv("GATEWAY_CLIENT_ID", "c-m2m")
        monkeypatch.setenv("GATEWAY_CLIENT_SECRET", "s3cret")

        monkeypatch.setattr(
            gateway, "_token_from_idp_directly",
            lambda endpoint, cid, secret: f"token-for-{cid}",
        )
        monkeypatch.setattr(
            gateway, "_token_from_identity",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("不该走 Identity")),
        )

        assert gateway.get_gateway_token(get_settings()) == "token-for-c-m2m"
        get_settings.cache_clear()

    def test_http_error_does_not_leak_the_secret(self, monkeypatch):
        """IdP 返回 4xx 时的报错里不能出现 client_secret。"""
        import urllib.error

        import agent.tools.gateway as gateway

        canary = "SECRET-CANARY-7f3a"

        def boom(request, timeout=None):
            raise urllib.error.HTTPError(
                "https://idp.test/oauth2/token", 401, "Unauthorized", {}, None
            )

        monkeypatch.setattr(gateway.urllib.request, "urlopen", boom)

        with pytest.raises(RuntimeError) as excinfo:
            gateway._token_from_idp_directly(
                "https://idp.test/oauth2/token", "c-m2m", canary
            )
        assert canary not in str(excinfo.value)
        assert "401" in str(excinfo.value)
