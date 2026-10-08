#!/usr/bin/env python3
"""验证 Demo OAuth 2.1 Remote MCP 全链路，不输出密码、code 或 token。

流程：RFC9728/RFC8414 discovery → DCR → 登录页 → Authorization Code + PKCE
→ token exchange → MCP initialize/tools/list。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import secrets
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from urllib.error import HTTPError, URLError

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "src"))

from demo_web import load_dotenv  # noqa: E402


class VerificationError(RuntimeError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def get_json(url: str) -> dict:
    try:
        with urllib.request.urlopen(url, timeout=20) as response:
            payload = json.load(response)
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise VerificationError(f"无法读取 metadata:{url}") from exc
    if not isinstance(payload, dict):
        raise VerificationError("metadata 不是 JSON object")
    return payload


def post_json(url: str, payload: dict) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            result = json.load(response)
    except HTTPError as exc:
        raise VerificationError(f"DCR 返回 HTTP {exc.code}") from None
    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise VerificationError("DCR 请求失败") from exc
    if not isinstance(result, dict):
        raise VerificationError("DCR 响应格式错误")
    return result


def post_form(url: str, payload: dict, *, no_redirect: bool = False) -> tuple[int, dict[str, str], bytes]:
    request = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(payload).encode(),
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    opener = urllib.request.build_opener(NoRedirect()) if no_redirect else urllib.request.build_opener()
    try:
        with opener.open(request, timeout=25) as response:
            return response.status, dict(response.headers), response.read()
    except HTTPError as exc:
        if no_redirect and exc.code in {301, 302, 303, 307, 308}:
            return exc.code, dict(exc.headers), exc.read()
        raise VerificationError(f"表单请求返回 HTTP {exc.code}") from None
    except (URLError, TimeoutError) as exc:
        raise VerificationError("表单请求失败") from exc


def verify_tools(gateway_url: str, token: str) -> tuple[list[str], str]:
    """验证 tools/list 并实际调用只读 get_order，避免只验握手。"""
    import asyncio

    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async def run() -> tuple[list[str], str]:
        async with streamablehttp_client(
            gateway_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        ) as (read, write, _session_id):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listed = await session.list_tools()
                names = sorted(tool.name for tool in listed.tools)
                order_tool = next((name for name in names if name.endswith("get_order")), "")
                if not order_tool:
                    raise VerificationError("Gateway 缺少 get_order 工具")
                result = await session.call_tool(order_tool, {"order_id": "ORD-1024"})
                if result.isError:
                    raise VerificationError("get_order 返回 MCP error")
                text = "\n".join(
                    getattr(item, "text", "") for item in result.content
                    if getattr(item, "text", "")
                )
                if "ORD-1024" not in text:
                    raise VerificationError("get_order 未返回 ORD-1024")
                return names, text

    return asyncio.run(run())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--redirect-uri",
        default="http://127.0.0.1:48999/oauth/callback",
        help="DCR 测试回调；默认使用不会真正监听的 loopback URI",
    )
    args = parser.parse_args(argv)
    load_dotenv(REPO_ROOT / ".env")

    gateway_url = os.environ.get("GATEWAY_URL", "").rstrip("/")
    username = os.environ.get("DEMO_USERNAME", "")
    password = os.environ.get("DEMO_PASSWORD", "")
    if not gateway_url or not username or not password:
        print(".env 缺少 GATEWAY_URL / DEMO_USERNAME / DEMO_PASSWORD", file=sys.stderr)
        return 1

    try:
        base = gateway_url[: -len("/mcp")] if gateway_url.endswith("/mcp") else ""
        if not base:
            raise VerificationError("GATEWAY_URL 必须以 /mcp 结尾")
        resource_metadata = get_json(f"{base}/.well-known/oauth-protected-resource")
        issuer = (resource_metadata.get("authorization_servers") or [""])[0].rstrip("/")
        resource = resource_metadata.get("resource")
        scopes = resource_metadata.get("scopes_supported") or []
        if not issuer or resource != gateway_url or not scopes:
            raise VerificationError("Protected Resource Metadata 不完整")
        auth_metadata = get_json(f"{issuer}/.well-known/oauth-authorization-server")
        for field in ("registration_endpoint", "authorization_endpoint", "token_endpoint"):
            if not auth_metadata.get(field):
                raise VerificationError(f"Authorization metadata 缺少 {field}")
        if "S256" not in (auth_metadata.get("code_challenge_methods_supported") or []):
            raise VerificationError("Authorization Server 未声明 PKCE S256")
        print("1/6 OAuth metadata: OK")

        registration = post_json(
            auth_metadata["registration_endpoint"],
            {
                "client_name": "agentcore-cn OAuth verifier",
                "redirect_uris": [args.redirect_uri],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
            },
        )
        client_id = registration.get("client_id")
        if not client_id or registration.get("client_secret"):
            raise VerificationError("DCR 没有返回预期的 public client")
        print("2/6 Dynamic Client Registration: OK")

        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()
        ).rstrip(b"=").decode()
        auth_params = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": args.redirect_uri,
            "scope": " ".join(scopes),
            "state": secrets.token_urlsafe(24),
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "resource": resource,
        }
        authorize_url = auth_metadata["authorization_endpoint"] + "?" + urllib.parse.urlencode(auth_params)
        try:
            with urllib.request.urlopen(authorize_url, timeout=20) as response:
                login_html = response.read().decode("utf-8", errors="replace")
        except (HTTPError, URLError, TimeoutError) as exc:
            raise VerificationError("Authorization login page 不可用") from exc
        if 'type="password"' not in login_html or "登录并授权" not in login_html:
            raise VerificationError("Authorization endpoint 没有返回登录页")
        print("3/6 Authorization login page: OK")

        status, headers, _ = post_form(
            auth_metadata["authorization_endpoint"],
            {**auth_params, "username": username, "password": password},
            no_redirect=True,
        )
        location = headers.get("Location") or headers.get("location") or ""
        if status not in {302, 303} or not location:
            raise VerificationError("登录后没有返回 OAuth callback redirect")
        callback = urllib.parse.parse_qs(urllib.parse.urlsplit(location).query)
        if callback.get("state") != [auth_params["state"]] or not callback.get("code"):
            raise VerificationError("OAuth callback 缺少 code 或 state 不匹配")
        code = callback["code"][0]
        print("4/6 Authorization Code + state: OK")

        status, _, raw = post_form(
            auth_metadata["token_endpoint"],
            {
                "grant_type": "authorization_code",
                "client_id": client_id,
                "code": code,
                "redirect_uri": args.redirect_uri,
                "code_verifier": verifier,
                "resource": resource,
            },
        )
        if status != 200:
            raise VerificationError("Authorization Code token exchange 失败")
        token_payload = json.loads(raw)
        token = token_payload.get("access_token")
        if not token or not token_payload.get("refresh_token"):
            raise VerificationError("Token 响应缺少 access/refresh token")
        print("5/6 PKCE token exchange + refresh token: OK")

        tools, _order_result = verify_tools(gateway_url, token)
        if not tools:
            raise VerificationError("Gateway 未返回 MCP tools")
        print(f"6/6 Gateway MCP: OK ({len(tools)} tools, get_order ORD-1024 OK)")
        for name in tools:
            print(f"  {name}")
    except (VerificationError, json.JSONDecodeError) as exc:
        print(f"验证失败:{exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"验证失败:{type(exc).__name__}（敏感详情已隐藏）", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
