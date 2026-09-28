"""自建迷你 OIDC Provider。

为什么要自己写:中国区 AgentCore 的 Runtime 和 Gateway 都不支持 Cognito 入向鉴权,
Gateway 还禁止 "No authorization",Identity 也没有 Private IdP。想用 CUSTOM_JWT
就必须自备一个 OIDC 兼容的 issuer。这个 Lambda 就是那个 issuer。

设计约束:
  * 零第三方依赖 —— 只用标准库 + Lambda 自带的 boto3,不打 Layer。
    JWT 手工拼接,签名交给 KMS,JWKS 用纯 stdlib 解析 KMS 返回的 DER 公钥。
  * 私钥永不落地 —— 签名私钥留在 KMS 里,DynamoDB 只存用户/客户端/吊销状态。

暴露的端点(HTTP API,$default stage,所以路径就是根路径):
  GET  /.well-known/openid-configuration
  GET  /.well-known/jwks.json
  POST /oauth2/token          grant_type = password | client_credentials | refresh_token
  POST /oauth2/introspect
  POST /oauth2/revoke
  GET  /oauth2/authorize      仅为 discovery 完整性保留,返回 400

这个 issuer 同时服务三个地方:
  1. Runtime 入向 CUSTOM_JWT
  2. Gateway 入向 CUSTOM_JWT
  3. Identity 出向 OAuth2 provider(client_credentials),给 Gateway 的 OpenAPI target 用
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
import urllib.parse
import uuid
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

LOG = logging.getLogger()
LOG.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

AUTH_TABLE = os.environ["AUTH_TABLE"]
KMS_KEY_ID = os.environ["KMS_KEY_ID"]
# ISSUER 必须与 discovery 文档里的 issuer 和 token 的 iss 完全一致,
# 否则 AgentCore 侧校验会失败。部署脚本在拿到 API 域名后回填。
ISSUER = os.environ["ISSUER"].rstrip("/")
DEFAULT_AUDIENCE = os.environ.get("DEFAULT_AUDIENCE", "agentcore-cn")

ACCESS_TOKEN_TTL = int(os.environ.get("ACCESS_TOKEN_TTL", "3600"))
REFRESH_TOKEN_TTL = int(os.environ.get("REFRESH_TOKEN_TTL", str(7 * 24 * 3600)))

PBKDF2_ROUNDS = 210_000
PBKDF2_PREFIX = "pbkdf2_sha256"

_BOTO_CFG = Config(retries={"max_attempts": 3, "mode": "standard"})
_ddb = boto3.client("dynamodb", config=_BOTO_CFG)
_kms = boto3.client("kms", config=_BOTO_CFG)

# Lambda 容器复用期内缓存公钥,省掉每次 GetPublicKey 的往返
_jwks_cache: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# base64url / DER 工具
# ---------------------------------------------------------------------------


def b64u_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def b64u_decode(data: str) -> bytes:
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


def _der_read(buf: bytes, offset: int) -> tuple[int, bytes, int]:
    """读一个 DER TLV,返回 (tag, value, 下一个 offset)。

    只实现 JWKS 需要的部分:SEQUENCE / BIT STRING / INTEGER,
    支持短格式和长格式长度。
    """
    if offset + 2 > len(buf):
        raise ValueError("DER truncated at header")
    tag = buf[offset]
    length = buf[offset + 1]
    cursor = offset + 2
    if length & 0x80:
        n_bytes = length & 0x7F
        if n_bytes == 0 or n_bytes > 4:
            raise ValueError(f"unsupported DER length form: {n_bytes} bytes")
        if cursor + n_bytes > len(buf):
            raise ValueError("DER truncated in long-form length")
        length = int.from_bytes(buf[cursor : cursor + n_bytes], "big")
        cursor += n_bytes
    if cursor + length > len(buf):
        raise ValueError("DER truncated in value")
    return tag, buf[cursor : cursor + length], cursor + length


def rsa_spki_to_jwk_params(spki_der: bytes) -> tuple[bytes, bytes]:
    """从 SubjectPublicKeyInfo DER 里抠出 RSA 的 modulus 和 exponent。

    SubjectPublicKeyInfo ::= SEQUENCE {
        algorithm        AlgorithmIdentifier,
        subjectPublicKey BIT STRING          -- 内含 RSAPublicKey 的 DER
    }
    RSAPublicKey ::= SEQUENCE { modulus INTEGER, publicExponent INTEGER }

    用纯标准库做,免得为了一个解析引入 cryptography 依赖。
    """
    tag, spki_body, _ = _der_read(spki_der, 0)
    if tag != 0x30:
        raise ValueError("SPKI: outer element is not a SEQUENCE")

    # 跳过 AlgorithmIdentifier
    tag, _, cursor = _der_read(spki_body, 0)
    if tag != 0x30:
        raise ValueError("SPKI: AlgorithmIdentifier is not a SEQUENCE")

    tag, bit_string, _ = _der_read(spki_body, cursor)
    if tag != 0x03:
        raise ValueError("SPKI: subjectPublicKey is not a BIT STRING")
    if not bit_string or bit_string[0] != 0x00:
        raise ValueError("SPKI: unexpected unused-bits byte in BIT STRING")

    tag, rsa_body, _ = _der_read(bit_string[1:], 0)
    if tag != 0x30:
        raise ValueError("RSAPublicKey is not a SEQUENCE")

    tag, modulus, cursor = _der_read(rsa_body, 0)
    if tag != 0x02:
        raise ValueError("RSAPublicKey.modulus is not an INTEGER")
    tag, exponent, _ = _der_read(rsa_body, cursor)
    if tag != 0x02:
        raise ValueError("RSAPublicKey.publicExponent is not an INTEGER")

    # DER INTEGER 是有符号的,正数高位为 1 时会补一个 0x00,JWK 里要去掉
    return modulus.lstrip(b"\x00") or b"\x00", exponent.lstrip(b"\x00") or b"\x00"


def get_jwks() -> dict[str, Any]:
    """构造 JWKS。kid 从公钥内容派生,保证与签名时写入 header 的 kid 一致。"""
    global _jwks_cache
    if _jwks_cache is not None:
        return _jwks_cache

    resp = _kms.get_public_key(KeyId=KMS_KEY_ID)
    spki_der = resp["PublicKey"]
    modulus, exponent = rsa_spki_to_jwk_params(spki_der)
    kid = b64u_encode(hashlib.sha256(spki_der).digest()[:16])

    _jwks_cache = {
        "keys": [
            {
                "kty": "RSA",
                "use": "sig",
                "alg": "RS256",
                "kid": kid,
                "n": b64u_encode(modulus),
                "e": b64u_encode(exponent),
            }
        ]
    }
    return _jwks_cache


def current_kid() -> str:
    return get_jwks()["keys"][0]["kid"]


# ---------------------------------------------------------------------------
# 密码哈希(与 scripts/seed_auth.py 共用)
# ---------------------------------------------------------------------------


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS)
    return f"{PBKDF2_PREFIX}${PBKDF2_ROUNDS}${b64u_encode(salt)}${b64u_encode(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, rounds_s, salt_s, digest_s = stored.split("$")
        if scheme != PBKDF2_PREFIX:
            return False
        rounds = int(rounds_s)
        salt = b64u_decode(salt_s)
        expected = b64u_decode(digest_s)
    except (ValueError, binascii.Error):
        LOG.warning("stored credential is malformed")
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
    return hmac.compare_digest(actual, expected)


# ---------------------------------------------------------------------------
# JWT 签发
# ---------------------------------------------------------------------------


def sign_jwt(claims: dict[str, Any]) -> str:
    header = {"alg": "RS256", "typ": "JWT", "kid": current_kid()}
    signing_input = ".".join(
        (
            b64u_encode(json.dumps(header, separators=(",", ":"), sort_keys=True).encode()),
            b64u_encode(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode()),
        )
    )
    # MessageType=RAW 让 KMS 自己做 SHA-256,签名输入远小于 4096 字节上限
    resp = _kms.sign(
        KeyId=KMS_KEY_ID,
        Message=signing_input.encode("ascii"),
        MessageType="RAW",
        SigningAlgorithm="RSASSA_PKCS1_V1_5_SHA_256",
    )
    return f"{signing_input}.{b64u_encode(resp['Signature'])}"


def build_access_token(
    *,
    subject: str,
    client_id: str,
    scope: str,
    audience: str,
    extra: dict[str, Any] | None = None,
) -> tuple[str, str, int]:
    now = int(time.time())
    jti = str(uuid.uuid4())
    claims: dict[str, Any] = {
        "iss": ISSUER,
        "sub": subject,
        "aud": audience,
        "client_id": client_id,
        "scope": scope,
        "iat": now,
        "nbf": now,
        "exp": now + ACCESS_TOKEN_TTL,
        "jti": jti,
        "token_use": "access",
    }
    if extra:
        claims.update(extra)
    return sign_jwt(claims), jti, claims["exp"]


# ---------------------------------------------------------------------------
# DynamoDB 访问
# ---------------------------------------------------------------------------


def _get_item(pk: str, sk: str) -> dict[str, Any] | None:
    resp = _ddb.get_item(
        TableName=AUTH_TABLE,
        Key={"PK": {"S": pk}, "SK": {"S": sk}},
        ConsistentRead=True,
    )
    return resp.get("Item")


def load_user(username: str) -> dict[str, Any] | None:
    return _get_item(f"USER#{username}", "PROFILE")


def load_client(client_id: str) -> dict[str, Any] | None:
    return _get_item(f"CLIENT#{client_id}", "PROFILE")


def _hash_token(token: str) -> str:
    """refresh token 只存哈希,库被读走也换不出 token。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def store_refresh_token(token: str, *, subject: str, client_id: str, scope: str, audience: str) -> None:
    _ddb.put_item(
        TableName=AUTH_TABLE,
        Item={
            "PK": {"S": f"REFRESH#{_hash_token(token)}"},
            "SK": {"S": "TOKEN"},
            "subject": {"S": subject},
            "client_id": {"S": client_id},
            "scope": {"S": scope},
            "audience": {"S": audience},
            "expires_at": {"N": str(int(time.time()) + REFRESH_TOKEN_TTL)},
        },
    )


def consume_refresh_token(token: str) -> dict[str, Any] | None:
    """一次性使用:换过就删,防重放。"""
    key = {"PK": {"S": f"REFRESH#{_hash_token(token)}"}, "SK": {"S": "TOKEN"}}
    try:
        resp = _ddb.delete_item(TableName=AUTH_TABLE, Key=key, ReturnValues="ALL_OLD")
    except ClientError:
        LOG.exception("failed to consume refresh token")
        return None
    item = resp.get("Attributes")
    if not item:
        return None
    # TTL 清理有延迟,自己再判一次过期
    if int(item.get("expires_at", {}).get("N", "0")) <= int(time.time()):
        return None
    return item


def revoke_jti(jti: str, expires_at: int) -> None:
    _ddb.put_item(
        TableName=AUTH_TABLE,
        Item={
            "PK": {"S": f"JTI#{jti}"},
            "SK": {"S": "REVOKED"},
            "expires_at": {"N": str(expires_at)},
        },
    )


def is_jti_revoked(jti: str) -> bool:
    return _get_item(f"JTI#{jti}", "REVOKED") is not None


# ---------------------------------------------------------------------------
# HTTP 响应工具
# ---------------------------------------------------------------------------


def _response(status: int, body: Any, *, cache_seconds: int = 0) -> dict[str, Any]:
    headers = {"Content-Type": "application/json"}
    if cache_seconds:
        headers["Cache-Control"] = f"public, max-age={cache_seconds}"
    else:
        headers["Cache-Control"] = "no-store"
    return {
        "statusCode": status,
        "headers": headers,
        "body": json.dumps(body, separators=(",", ":")),
    }


def _oauth_error(status: int, code: str, description: str) -> dict[str, Any]:
    # 描述保持笼统,不回显用户是否存在,避免用户名枚举
    return _response(status, {"error": code, "error_description": description})


# ---------------------------------------------------------------------------
# 端点实现
# ---------------------------------------------------------------------------


def handle_discovery() -> dict[str, Any]:
    return _response(
        200,
        {
            "issuer": ISSUER,
            "jwks_uri": f"{ISSUER}/.well-known/jwks.json",
            "authorization_endpoint": f"{ISSUER}/oauth2/authorize",
            "token_endpoint": f"{ISSUER}/oauth2/token",
            "introspection_endpoint": f"{ISSUER}/oauth2/introspect",
            "revocation_endpoint": f"{ISSUER}/oauth2/revoke",
            "grant_types_supported": ["password", "client_credentials", "refresh_token"],
            "response_types_supported": ["token"],
            "subject_types_supported": ["public"],
            "id_token_signing_alg_values_supported": ["RS256"],
            "token_endpoint_auth_methods_supported": [
                "client_secret_basic",
                "client_secret_post",
            ],
            "scopes_supported": ["agent:invoke", "gateway:invoke", "tools:read", "tools:write"],
            "claims_supported": [
                "iss",
                "sub",
                "aud",
                "exp",
                "iat",
                "jti",
                "scope",
                "client_id",
                "actor_id",
                "username",
            ],
        },
        cache_seconds=300,
    )


def handle_jwks() -> dict[str, Any]:
    return _response(200, get_jwks(), cache_seconds=300)


def _parse_form(event: dict[str, Any]) -> dict[str, str]:
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8")
    content_type = ""
    for key, value in (event.get("headers") or {}).items():
        if key.lower() == "content-type":
            content_type = value.lower()
            break
    if "application/json" in content_type:
        try:
            parsed = json.loads(body or "{}")
        except json.JSONDecodeError:
            return {}
        return {k: str(v) for k, v in parsed.items()} if isinstance(parsed, dict) else {}
    return {k: v[0] for k, v in urllib.parse.parse_qs(body).items()}


def _client_credentials_from_request(event: dict[str, Any], form: dict[str, str]) -> tuple[str, str]:
    """支持 client_secret_basic 和 client_secret_post 两种方式。"""
    for key, value in (event.get("headers") or {}).items():
        if key.lower() != "authorization":
            continue
        if not value.lower().startswith("basic "):
            break
        try:
            decoded = base64.b64decode(value[6:].strip()).decode("utf-8")
            client_id, _, client_secret = decoded.partition(":")
            # Basic 里的值是 form-urlencoded 的(RFC 6749 §2.3.1)
            return urllib.parse.unquote(client_id), urllib.parse.unquote(client_secret)
        except (binascii.Error, UnicodeDecodeError):
            break
    return form.get("client_id", ""), form.get("client_secret", "")


def _authenticate_client(client_id: str, client_secret: str) -> dict[str, Any] | None:
    if not client_id or not client_secret:
        return None
    item = load_client(client_id)
    if not item:
        # 即使客户端不存在也走一遍哈希,抹平时间差
        verify_password(client_secret, hash_password(secrets.token_urlsafe(16)))
        return None
    if not verify_password(client_secret, item.get("secret_hash", {}).get("S", "")):
        return None
    return item


def _allowed_scopes(item: dict[str, Any]) -> set[str]:
    return set(item.get("scopes", {}).get("SS", []) or [])


def _resolve_scope(requested: str, allowed: set[str]) -> str | None:
    """没请求就给全量;请求了就必须是允许集合的子集。"""
    if not requested:
        return " ".join(sorted(allowed))
    wanted = set(requested.split())
    if not wanted.issubset(allowed):
        return None
    return " ".join(sorted(wanted))


def handle_token(event: dict[str, Any]) -> dict[str, Any]:
    form = _parse_form(event)
    grant_type = form.get("grant_type", "")
    client_id, client_secret = _client_credentials_from_request(event, form)

    client = _authenticate_client(client_id, client_secret)
    if client is None:
        return _oauth_error(401, "invalid_client", "client authentication failed")

    grant_types = set(client.get("grant_types", {}).get("SS", []) or [])
    if grant_type not in grant_types:
        return _oauth_error(400, "unauthorized_client", "grant type not allowed for this client")

    audience = client.get("audience", {}).get("S", DEFAULT_AUDIENCE)
    client_scopes = _allowed_scopes(client)

    if grant_type == "client_credentials":
        scope = _resolve_scope(form.get("scope", ""), client_scopes)
        if scope is None:
            return _oauth_error(400, "invalid_scope", "requested scope exceeds client grant")
        token, _, exp = build_access_token(
            subject=f"client:{client_id}",
            client_id=client_id,
            scope=scope,
            audience=audience,
        )
        return _response(
            200,
            {
                "access_token": token,
                "token_type": "Bearer",
                "expires_in": exp - int(time.time()),
                "scope": scope,
            },
        )

    if grant_type == "password":
        username = form.get("username", "")
        password = form.get("password", "")
        user = load_user(username) if username else None
        if user is None or not password:
            verify_password(password or "x", hash_password(secrets.token_urlsafe(16)))
            return _oauth_error(400, "invalid_grant", "invalid username or password")
        if not verify_password(password, user.get("password_hash", {}).get("S", "")):
            return _oauth_error(400, "invalid_grant", "invalid username or password")
        if user.get("disabled", {}).get("BOOL", False):
            return _oauth_error(400, "invalid_grant", "invalid username or password")

        # 最终 scope = 客户端允许 ∩ 用户拥有
        effective = client_scopes & _allowed_scopes(user)
        scope = _resolve_scope(form.get("scope", ""), effective)
        if scope is None:
            return _oauth_error(400, "invalid_scope", "requested scope exceeds granted scope")

        actor_id = user.get("actor_id", {}).get("S", username)
        token, _, exp = build_access_token(
            subject=actor_id,
            client_id=client_id,
            scope=scope,
            audience=audience,
            # actor_id 会被 Agent 用来隔离 MemoryLite 的数据
            extra={"actor_id": actor_id, "username": username},
        )
        refresh = secrets.token_urlsafe(48)
        store_refresh_token(
            refresh, subject=actor_id, client_id=client_id, scope=scope, audience=audience
        )
        return _response(
            200,
            {
                "access_token": token,
                "token_type": "Bearer",
                "expires_in": exp - int(time.time()),
                "refresh_token": refresh,
                "scope": scope,
            },
        )

    if grant_type == "refresh_token":
        presented = form.get("refresh_token", "")
        if not presented:
            return _oauth_error(400, "invalid_request", "refresh_token is required")
        record = consume_refresh_token(presented)
        if record is None:
            return _oauth_error(400, "invalid_grant", "refresh token is invalid or expired")
        if record.get("client_id", {}).get("S") != client_id:
            return _oauth_error(400, "invalid_grant", "refresh token was issued to another client")

        subject = record["subject"]["S"]
        scope = record.get("scope", {}).get("S", "")
        token, _, exp = build_access_token(
            subject=subject,
            client_id=client_id,
            scope=scope,
            audience=record.get("audience", {}).get("S", audience),
            extra={"actor_id": subject},
        )
        rotated = secrets.token_urlsafe(48)
        store_refresh_token(
            rotated,
            subject=subject,
            client_id=client_id,
            scope=scope,
            audience=record.get("audience", {}).get("S", audience),
        )
        return _response(
            200,
            {
                "access_token": token,
                "token_type": "Bearer",
                "expires_in": exp - int(time.time()),
                "refresh_token": rotated,
                "scope": scope,
            },
        )

    return _oauth_error(400, "unsupported_grant_type", f"unsupported grant_type: {grant_type}")


def _peek_claims(token: str) -> dict[str, Any] | None:
    """只解 payload,不验签。仅用于 revoke —— 那里拿到 jti 就够了,
    而且按 RFC 7009 无论 token 有效与否都要返回 200,不做信任决策。

    任何需要信任 claims 的地方必须用 verify_jwt(),不要用这个。
    """
    try:
        _, payload_b64, _ = token.split(".")
        return json.loads(b64u_decode(payload_b64))
    except (ValueError, binascii.Error, json.JSONDecodeError):
        return None


def verify_jwt(token: str) -> dict[str, Any] | None:
    """完整校验:签名 -> issuer -> 时间窗 -> 吊销状态。

    验签交给 KMS Verify,这样 Lambda 里不需要任何 RSA 实现。
    """
    try:
        header_b64, payload_b64, signature_b64 = token.split(".")
        header = json.loads(b64u_decode(header_b64))
        claims = json.loads(b64u_decode(payload_b64))
        signature = b64u_decode(signature_b64)
    except (ValueError, binascii.Error, json.JSONDecodeError):
        return None

    if not isinstance(claims, dict) or header.get("alg") != "RS256":
        return None
    # kid 不匹配说明是别的密钥签的,直接拒,不去猜
    if header.get("kid") != current_kid():
        return None

    try:
        resp = _kms.verify(
            KeyId=KMS_KEY_ID,
            Message=f"{header_b64}.{payload_b64}".encode("ascii"),
            MessageType="RAW",
            Signature=signature,
            SigningAlgorithm="RSASSA_PKCS1_V1_5_SHA_256",
        )
    except ClientError:
        # 签名不匹配时 KMS 抛 KMSInvalidSignatureException
        return None
    if not resp.get("SignatureValid"):
        return None

    if claims.get("iss") != ISSUER:
        return None
    now = int(time.time())
    try:
        if int(claims.get("exp", 0)) <= now or int(claims.get("nbf", 0)) > now:
            return None
    except (TypeError, ValueError):
        return None
    if is_jti_revoked(str(claims.get("jti", ""))):
        return None
    return claims


def handle_introspect(event: dict[str, Any]) -> dict[str, Any]:
    form = _parse_form(event)
    client_id, client_secret = _client_credentials_from_request(event, form)
    if _authenticate_client(client_id, client_secret) is None:
        return _oauth_error(401, "invalid_client", "client authentication failed")

    claims = verify_jwt(form.get("token", ""))
    if claims is None:
        return _response(200, {"active": False})
    return _response(
        200,
        {
            "active": True,
            "sub": claims.get("sub"),
            "aud": claims.get("aud"),
            "scope": claims.get("scope"),
            "client_id": claims.get("client_id"),
            "exp": claims.get("exp"),
            "iat": claims.get("iat"),
            "jti": claims.get("jti"),
            "username": claims.get("username"),
        },
    )


def handle_revoke(event: dict[str, Any]) -> dict[str, Any]:
    form = _parse_form(event)
    client_id, client_secret = _client_credentials_from_request(event, form)
    if _authenticate_client(client_id, client_secret) is None:
        return _oauth_error(401, "invalid_client", "client authentication failed")

    token = form.get("token", "")
    # RFC 7009:无论 token 有效与否都返回 200
    claims = _peek_claims(token)
    if claims and claims.get("jti"):
        revoke_jti(str(claims["jti"]), int(claims.get("exp", int(time.time()) + 3600)))
    else:
        consume_refresh_token(token)
    return _response(200, {})


ROUTES: dict[tuple[str, str], Any] = {
    ("GET", "/.well-known/openid-configuration"): lambda event: handle_discovery(),
    ("GET", "/.well-known/jwks.json"): lambda event: handle_jwks(),
    # 有些客户端按 RFC 8414 找这个路径
    ("GET", "/.well-known/oauth-authorization-server"): lambda event: handle_discovery(),
    ("POST", "/oauth2/token"): handle_token,
    ("POST", "/oauth2/introspect"): handle_introspect,
    ("POST", "/oauth2/revoke"): handle_revoke,
    ("GET", "/oauth2/authorize"): lambda event: _oauth_error(
        400,
        "unsupported_response_type",
        "this demo IdP only supports password / client_credentials / refresh_token",
    ),
}


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    http = (event.get("requestContext") or {}).get("http") or {}
    method = http.get("method", event.get("httpMethod", "GET")).upper()
    path = http.get("path", event.get("rawPath", "/"))

    handler = ROUTES.get((method, path))
    if handler is None:
        return _oauth_error(404, "not_found", f"no route for {method} {path}")

    try:
        return handler(event)
    except Exception:  # noqa: BLE001 - 最外层兜底,细节只进日志不回显
        LOG.exception("unhandled error on %s %s", method, path)
        return _oauth_error(500, "server_error", "internal error")
