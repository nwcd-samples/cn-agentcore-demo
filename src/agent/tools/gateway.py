"""通过 AgentCore Gateway 拿业务工具(MCP)。

Gateway 把 Lambda 暴露成 MCP 工具,这里用 Strands 的 MCPClient 连上去,
`list_tools_sync()` 拿到的工具可以直接塞给 Agent。

两个必须注意的点:

1. MCPClient 是上下文管理器,工具只在会话存活期间可调用。
   所以这里返回 (client, tools),由调用方用 ExitStack 管生命周期,
   不能拿完 tools 就把 client 丢掉 —— 那样工具调用会失败。

2. 入向鉴权是 CUSTOM_JWT,必须带 Bearer token。token 有两条来源:
   * AgentCore Identity 的 OAuth2 credential provider(线上,M2M 流)
   * 直接拿 client_credentials 打自建 IdP 的 token 端点(本地调试)
   Gateway 侧校验 aud 和 client_id,不匹配会直接拒,且错误很不明显。
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request

from strands.tools.mcp import MCPClient

from agent import obs
from agent.config import Settings, get_settings

LOG = logging.getLogger(__name__)

# Gateway 只需要调用工具的权限
GATEWAY_SCOPES = ["gateway:invoke", "tools:read", "tools:write"]

_cached_token: str | None = None


def _token_from_identity(provider_name: str, scopes: list[str]) -> str:
    """走 AgentCore Identity 的 OAuth2 provider(client_credentials / M2M)。

    同 model.py:装饰同步函数,让 SDK 自己处理事件循环和 ContextVar 传递。
    workload access token 存在 ContextVar 里,自己起线程会丢。
    """
    from bedrock_agentcore.identity.auth import requires_access_token

    @requires_access_token(provider_name=provider_name, scopes=scopes, auth_flow="M2M")
    def _grab(*, access_token: str) -> str:
        return access_token

    return _grab()


def _token_from_idp_directly(token_endpoint: str, client_id: str, client_secret: str) -> str:
    """本地调试用:直接拿 client_credentials 打自建 IdP。

    只在设置了 IDP_TOKEN_ENDPOINT + GATEWAY_CLIENT_ID/SECRET 时走这条路。
    """
    body = urllib.parse.urlencode(
        {"grant_type": "client_credentials", "scope": " ".join(GATEWAY_SCOPES)}
    ).encode()
    request = urllib.request.Request(token_endpoint, data=body, method="POST")
    request.add_header("Content-Type", "application/x-www-form-urlencoded")
    basic = urllib.parse.quote(client_id) + ":" + urllib.parse.quote(client_secret)
    import base64

    request.add_header(
        "Authorization", "Basic " + base64.b64encode(basic.encode()).decode()
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        # 不要把 client_secret 带进日志
        raise RuntimeError(
            f"自建 IdP 返回 {exc.code},检查 client_id 与 grant_types 配置"
        ) from None
    token = payload.get("access_token")
    if not token:
        raise RuntimeError("IdP 没有返回 access_token")
    return token


def get_gateway_token(settings: Settings | None = None, *, refresh: bool = False) -> str:
    global _cached_token
    settings = settings or get_settings()
    if _cached_token and not refresh:
        return _cached_token

    token_endpoint = os.environ.get("IDP_TOKEN_ENDPOINT", "").strip()
    client_id = os.environ.get("GATEWAY_CLIENT_ID", "").strip()
    client_secret = os.environ.get("GATEWAY_CLIENT_SECRET", "").strip()

    if token_endpoint and client_id and client_secret:
        LOG.info("Gateway token 直接取自自建 IdP(本地调试模式)")
        _cached_token = _token_from_idp_directly(token_endpoint, client_id, client_secret)
    else:
        LOG.info(
            "Gateway token 取自 AgentCore Identity,provider=%s",
            settings.gateway_oauth_provider,
        )
        _cached_token = _token_from_identity(
            settings.gateway_oauth_provider, GATEWAY_SCOPES
        )
    return _cached_token


def build_gateway_client(settings: Settings | None = None) -> MCPClient:
    """构造(但不启动)指向 Gateway 的 MCP 客户端。"""
    settings = settings or get_settings()
    if not settings.gateway_url:
        raise RuntimeError("GATEWAY_URL 未设置,先跑 scripts/create_gateway.py")

    token = get_gateway_token(settings)
    return MCPClient(
        url=settings.gateway_url,
        headers={"Authorization": f"Bearer {token}"},
        # Gateway 冷启动可能慢一点,给足握手时间
        startup_timeout=30,
        # 单个工具报错不要拖垮整个工具列表
        continue_on_error=True,
    )


def load_gateway_tools(settings: Settings | None = None) -> tuple[MCPClient, list]:
    """启动 MCP 会话并列出工具。

    返回 (client, tools)。调用方**必须**持有 client 直到不再调用工具为止,
    否则会话关闭后工具调用会失败。
    """
    settings = settings or get_settings()
    with obs.span("gateway.load_tools") as sp:
        client = build_gateway_client(settings)
        client.start()
        try:
            tools = list(client.list_tools_sync())
        except Exception:
            client.stop(None, None, None)
            raise
        sp["tool_count"] = len(tools)
    LOG.info("从 Gateway 取到 %d 个工具:%s", len(tools),
             [getattr(t, "tool_name", "?") for t in tools])
    return client, tools
