"""OAuth 客户端 ID 的命名约定。

三个脚本都要推导同一组客户端 ID:
  seed_auth.py      建客户端(写进 DynamoDB)
  create_gateway.py 把它们填进 CUSTOM_JWT 的 allowedClients
  setup_identity.py 用 m2m 那个建 OAuth2 credential provider

三处一旦不一致,症状是 Gateway 静默拒绝所有请求(allowedClients 不匹配),
而且错误信息完全看不出根因。所以推导逻辑集中在这里,由测试保证不漂移。

零依赖,能被三个脚本直接 import。
"""

from __future__ import annotations

import os

# 机器到机器客户端的后缀
M2M_SUFFIX = "-m2m"


def user_client_id(project: str) -> str:
    """交互式用户登录用的客户端(password + refresh_token)。"""
    return os.environ.get("DEMO_CLIENT_ID", "").strip() or f"{project}-client"


def m2m_client_id(project: str) -> str:
    """Gateway 出向用的客户端(client_credentials)。"""
    return user_client_id(project) + M2M_SUFFIX


def all_client_ids(project: str) -> list[str]:
    """填进 CUSTOM_JWT allowedClients 的完整列表。"""
    return [user_client_id(project), m2m_client_id(project)]


# ---------------------------------------------------------------------------
# Scope 约定
#
# 同样集中在这里。Agent 请求的 scope 必须是 m2m 客户端被授予的子集,
# 否则自建 IdP 会以 invalid_scope 拒掉 —— 而这个错误要翻 IdP 的日志才看得见。
# tests/test_identity.py 有断言保证 agent 侧和 seed 侧不漂移。
# ---------------------------------------------------------------------------

# 用户能被授予的上限
USER_SCOPES = ["agent:invoke", "tools:read", "tools:write"]

# 交互式客户端能请求的上限
USER_CLIENT_SCOPES = ["agent:invoke", "gateway:invoke", "tools:read", "tools:write"]

# Agent 调 Gateway 用的 m2m 客户端。需要 write —— 开工单是写操作。
M2M_CLIENT_SCOPES = ["gateway:invoke", "tools:read", "tools:write"]
