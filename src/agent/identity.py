"""从入向 JWT 里取调用者身份。

Runtime 配了 CUSTOM_JWT 之后,AgentCore 会先验签再把请求透给容器,
原始 Authorization 头也会一并带进来。容器侧**不需要再验签**
(那是 Runtime 的职责,而且容器拿不到 JWKS 也验不了),
但需要从里面读出 actor_id 来隔离 MemoryLite 的数据。

安全边界要讲清楚:
  这里解出来的 claims 只用于**数据分区**,不用于授权判定。
  授权已经在 Runtime 的 CUSTOM_JWT 那一层做完了 —— 签名不对的请求
  根本到不了这里。所以这里不验签是安全的;但反过来说,
  绝不能拿这里的 scope 去决定"能不能做某件事"。
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from dataclasses import dataclass

LOG = logging.getLogger(__name__)

# 拿不到身份时的兜底 actor,保证数据不会串到真实用户名下
ANONYMOUS_ACTOR = "anonymous"


@dataclass(frozen=True)
class Caller:
    actor_id: str
    username: str | None = None
    scopes: tuple[str, ...] = ()

    @property
    def is_anonymous(self) -> bool:
        return self.actor_id == ANONYMOUS_ACTOR


def _b64u_decode(segment: str) -> bytes:
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


def _claims_from_bearer(token: str) -> dict:
    try:
        _, payload_b64, _ = token.split(".")
        claims = json.loads(_b64u_decode(payload_b64))
    except (ValueError, binascii.Error, json.JSONDecodeError):
        LOG.warning("Authorization 头里不是一个能解析的 JWT")
        return {}
    return claims if isinstance(claims, dict) else {}


def caller_from_headers(headers: dict[str, str] | None) -> Caller:
    if not headers:
        return Caller(actor_id=ANONYMOUS_ACTOR)

    raw = ""
    for key, value in headers.items():
        if key.lower() == "authorization":
            raw = value
            break
    if not raw.lower().startswith("bearer "):
        # AWS_IAM 入向模式下没有 Bearer 头,这是正常路径
        return Caller(actor_id=ANONYMOUS_ACTOR)

    claims = _claims_from_bearer(raw[7:].strip())
    # 自建 IdP 在 password grant 里写了 actor_id;client_credentials 没有,回落到 sub
    actor_id = str(claims.get("actor_id") or claims.get("sub") or ANONYMOUS_ACTOR)
    scope = claims.get("scope") or ""
    return Caller(
        actor_id=actor_id,
        username=claims.get("username"),
        scopes=tuple(scope.split()) if isinstance(scope, str) else (),
    )
