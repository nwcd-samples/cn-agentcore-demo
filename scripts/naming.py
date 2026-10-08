"""OAuth 客户端 ID 与 scope 的统一约定。

所有会创建或放行客户端的脚本都必须调用这里，避免 IdP 中的 client_id 与
Gateway CUSTOM_JWT allowedClients 漂移。
"""

from __future__ import annotations

import os

M2M_SUFFIX = "-m2m"
QUICK_SUFFIX = "-quick"


def user_client_id(project: str) -> str:
    """交互式 Demo 用户客户端（password + refresh_token）。"""
    return os.environ.get("DEMO_CLIENT_ID", "").strip() or f"{project}-client"


def m2m_client_id(project: str) -> str:
    """Agent Runtime 调 Gateway 的机器客户端。"""
    return (
        os.environ.get("GATEWAY_CLIENT_ID", "").strip()
        or user_client_id(project) + M2M_SUFFIX
    )


def quick_client_id(project: str) -> str:
    """Amazon Quick 团队级 Remote MCP 的独立 Service-to-Service 客户端。"""
    return os.environ.get("QUICK_CLIENT_ID", "").strip() or f"{project}{QUICK_SUFFIX}"


def runtime_client_ids(project: str) -> list[str]:
    """Runtime 入向只允许交互用户和 Runtime 原有 M2M，不允许 Quick。"""
    return [user_client_id(project), m2m_client_id(project)]


def gateway_client_ids(project: str) -> list[str]:
    """Gateway 额外允许 Amazon Quick 团队级 MCP 客户端。"""
    client_ids = [*runtime_client_ids(project), quick_client_id(project)]
    if len(set(client_ids)) != len(client_ids):
        raise ValueError(f"OAuth client ID 必须互不相同:{client_ids}")
    return client_ids


def all_client_ids(project: str) -> list[str]:
    """兼容旧调用；等同于 Gateway 的完整客户端列表。"""
    return gateway_client_ids(project)


# 用户能被授予的上限
USER_SCOPES = ["agent:invoke", "tools:read", "tools:write"]

# 交互式客户端能请求的上限
USER_CLIENT_SCOPES = ["agent:invoke", "gateway:invoke", "tools:read", "tools:write"]

# Agent Runtime 调 Gateway。create_ticket 需要 tools:write。
M2M_CLIENT_SCOPES = ["gateway:invoke", "tools:read", "tools:write"]

# Quick 团队级 MCP 同样需要列工具、查询与可选写操作，但使用独立凭证，
# 这样轮换或吊销 Quick 不会影响 Runtime。
QUICK_CLIENT_SCOPES = ["gateway:invoke", "tools:read", "tools:write"]
