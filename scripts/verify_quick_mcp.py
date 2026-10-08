#!/usr/bin/env python3
"""输出并验证 Amazon Quick 团队级 Remote MCP（Service-to-Service）配置。

脚本不会打印 QUICK_CLIENT_SECRET 或 access token。默认只显示应填入 Quick 的
非敏感字段；--verify 会真实执行 client_credentials、MCP initialize 和 tools/list。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "src"))

import naming  # noqa: E402
from demo_web import load_dotenv  # noqa: E402

SCOPES = naming.QUICK_CLIENT_SCOPES


class QuickConfigError(RuntimeError):
    pass


def _get_json(url: str) -> dict:
    try:
        with urlopen(url, timeout=20) as response:
            payload = json.load(response)
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise QuickConfigError(f"无法读取 OAuth metadata:{url}") from exc
    if not isinstance(payload, dict):
        raise QuickConfigError(f"OAuth metadata 格式不正确:{url}")
    return payload


def discover_token_endpoint(gateway_url: str) -> str:
    gateway_url = gateway_url.rstrip("/")
    if not gateway_url.startswith("https://") or not gateway_url.endswith("/mcp"):
        raise QuickConfigError("GATEWAY_URL 必须是 https://.../mcp")
    base = gateway_url[: -len("/mcp")]
    resource = _get_json(f"{base}/.well-known/oauth-protected-resource")
    servers = resource.get("authorization_servers") or []
    if not servers or not isinstance(servers[0], str):
        raise QuickConfigError("Gateway resource metadata 缺少 authorization_servers")
    issuer = servers[0].rstrip("/")
    metadata = _get_json(f"{issuer}/.well-known/oauth-authorization-server")
    endpoint = metadata.get("token_endpoint")
    if not isinstance(endpoint, str) or not endpoint.startswith("https://"):
        raise QuickConfigError("Authorization Server metadata 缺少 token_endpoint")
    return endpoint


def request_token(
    token_endpoint: str,
    *,
    client_id: str,
    client_secret: str,
) -> str:
    from urllib.parse import urlencode

    body = urlencode(
        {
            "grant_type": "client_credentials",
            "scope": " ".join(SCOPES),
        }
    ).encode("utf-8")
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode("ascii")
    request = Request(
        token_endpoint,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
    )
    try:
        with urlopen(request, timeout=25) as response:
            payload = json.load(response)
    except HTTPError as exc:
        raise QuickConfigError(
            f"IdP 返回 HTTP {exc.code}；检查 Quick client 是否已 seed、secret 是否匹配"
        ) from None
    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise QuickConfigError("无法从 IdP 获取 Quick access token") from exc
    token = payload.get("access_token")
    if not isinstance(token, str) or not token:
        raise QuickConfigError("IdP 响应缺少 access_token")
    return token


def verify_tools(gateway_url: str, token: str) -> list[str]:
    from strands.tools.mcp import MCPClient

    client = MCPClient(
        url=gateway_url,
        headers={"Authorization": f"Bearer {token}"},
        startup_timeout=30,
        continue_on_error=True,
    )
    client.start()
    try:
        return sorted(getattr(tool, "tool_name", "?") for tool in client.list_tools_sync())
    finally:
        client.stop(None, None, None)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify", action="store_true", help="真实换 token 并列出 MCP 工具")
    args = parser.parse_args(argv)

    load_dotenv(REPO_ROOT / ".env")
    project = os.environ.get("PROJECT", "agentcore-cn")
    gateway_url = os.environ.get("GATEWAY_URL", "").strip()
    if not gateway_url:
        print("缺少 GATEWAY_URL", file=sys.stderr)
        return 1
    client_id = naming.quick_client_id(project)
    client_secret = os.environ.get("QUICK_CLIENT_SECRET", "").strip()

    try:
        token_endpoint = (
            os.environ.get("IDP_TOKEN_ENDPOINT", "").strip()
            or discover_token_endpoint(gateway_url)
        )
    except QuickConfigError as exc:
        print(f"配置错误:{exc}", file=sys.stderr)
        return 1

    print("Amazon Quick 团队级 Remote MCP 配置:")
    print(f"  MCP server endpoint: {gateway_url}")
    print("  Connection type:     Public network")
    print("  Authentication:      Service-to-Service")
    print(f"  Client ID:           {client_id}")
    print(f"  Client Secret:       {'已配置' if client_secret else '未配置'}")
    print(f"  Token URL:           {token_endpoint}")
    print(f"  Scope:               {' '.join(SCOPES)}")

    if not args.verify:
        return 0
    if not client_secret:
        print("验证失败:.env 缺少 QUICK_CLIENT_SECRET", file=sys.stderr)
        return 1

    try:
        token = request_token(
            token_endpoint,
            client_id=client_id,
            client_secret=client_secret,
        )
        tools = verify_tools(gateway_url, token)
    except QuickConfigError as exc:
        print(f"验证失败:{exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"验证失败:{type(exc).__name__}（敏感详情已隐藏）", file=sys.stderr)
        return 1

    print(f"\n验证通过：IdP 已签发 Token，Gateway 返回 {len(tools)} 个工具")
    for tool in tools:
        print(f"  {tool}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
