"""自建 OIDC IdP 的单元测试。

重点验证三件容易写错、且线上错了很难查的事:
  1. 手工解析 KMS 的 DER 公钥,n/e 必须和真实值一致(JWKS 错了 AgentCore 一律 401)
  2. 手工拼的 JWT 签名必须能被标准 RS256 验签流程验过
  3. 各 grant 的鉴权与 scope 边界不能放过不该放过的请求
"""

from __future__ import annotations

import base64
import json
import time
import urllib.parse

import pytest
import rsa_stub


# ---------------------------------------------------------------------------
# 构造 API Gateway HTTP API(payload 2.0)事件
# ---------------------------------------------------------------------------


def make_event(
    method: str,
    path: str,
    *,
    form: dict[str, str] | None = None,
    json_body: dict | None = None,
    basic_auth: tuple[str, str] | None = None,
    base64_encode: bool = False,
) -> dict:
    headers: dict[str, str] = {}
    body = ""
    if form is not None:
        headers["content-type"] = "application/x-www-form-urlencoded"
        body = urllib.parse.urlencode(form)
    elif json_body is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(json_body)
    if basic_auth is not None:
        user, secret = basic_auth
        raw = f"{urllib.parse.quote(user)}:{urllib.parse.quote(secret)}".encode()
        headers["Authorization"] = "Basic " + base64.b64encode(raw).decode()

    event: dict = {
        "requestContext": {"http": {"method": method, "path": path}},
        "headers": headers,
        "body": body,
        "isBase64Encoded": False,
    }
    if base64_encode and body:
        event["body"] = base64.b64encode(body.encode()).decode()
        event["isBase64Encoded"] = True
    return event


def body_of(response: dict) -> dict:
    return json.loads(response["body"])


def seed_client(
    idp,
    client_id: str = "agentcore-demo-client",
    secret: str = "s3cret-client-value",
    grant_types: tuple[str, ...] = ("password", "client_credentials", "refresh_token"),
    scopes: tuple[str, ...] = ("agent:invoke", "gateway:invoke", "tools:read", "tools:write"),
    audience: str = "agentcore-cn",
) -> tuple[str, str]:
    handler, ddb, _ = idp
    ddb.put_item(
        TableName="t",
        Item={
            "PK": {"S": f"CLIENT#{client_id}"},
            "SK": {"S": "PROFILE"},
            "secret_hash": {"S": handler.hash_password(secret)},
            "grant_types": {"SS": list(grant_types)},
            "scopes": {"SS": list(scopes)},
            "audience": {"S": audience},
        },
    )
    return client_id, secret


def seed_user(
    idp,
    username: str = "demo-user",
    password: str = "demo-password-value",
    scopes: tuple[str, ...] = ("agent:invoke", "tools:read"),
    actor_id: str = "actor-demo-001",
    disabled: bool = False,
) -> tuple[str, str]:
    handler, ddb, _ = idp
    ddb.put_item(
        TableName="t",
        Item={
            "PK": {"S": f"USER#{username}"},
            "SK": {"S": "PROFILE"},
            "password_hash": {"S": handler.hash_password(password)},
            "scopes": {"SS": list(scopes)},
            "actor_id": {"S": actor_id},
            "disabled": {"BOOL": disabled},
        },
    )
    return username, password


# ---------------------------------------------------------------------------
# DER 解析 / JWKS
# ---------------------------------------------------------------------------


class TestJwks:
    def test_der_parser_recovers_exact_modulus_and_exponent(self, idp, rsa_key):
        """这是整个 IdP 最脆的一环:n/e 差一个字节,AgentCore 就永远验不过签。"""
        handler, _, _ = idp
        der = rsa_stub.public_key_to_spki_der(rsa_key)

        modulus, exponent = handler.rsa_spki_to_jwk_params(der)

        assert int.from_bytes(modulus, "big") == rsa_key.n
        assert int.from_bytes(exponent, "big") == rsa_key.e

    def test_der_parser_handles_long_form_length(self, idp):
        """2048 位密钥的 modulus 是 257 字节,长度必须走长格式编码。"""
        handler, _, _ = idp
        big_key = rsa_stub.generate_key(2048)
        der = rsa_stub.public_key_to_spki_der(big_key)

        modulus, exponent = handler.rsa_spki_to_jwk_params(der)

        assert int.from_bytes(modulus, "big") == big_key.n
        assert int.from_bytes(exponent, "big") == big_key.e
        assert len(modulus) == 256

    def test_der_parser_rejects_garbage(self, idp):
        handler, _, _ = idp
        with pytest.raises(ValueError):
            handler.rsa_spki_to_jwk_params(b"\x30\x03\x02\x01\x00")

    def test_jwks_document_shape(self, idp):
        handler, _, _ = idp
        response = handler.lambda_handler(
            make_event("GET", "/.well-known/jwks.json"), None
        )

        assert response["statusCode"] == 200
        key = body_of(response)["keys"][0]
        assert key["kty"] == "RSA"
        assert key["alg"] == "RS256"
        assert key["use"] == "sig"
        assert key["kid"]
        # base64url:不能出现 + / =
        for field in ("n", "e"):
            assert not set(key[field]) & set("+/=")

    def test_public_key_is_cached_across_calls(self, idp):
        handler, _, fake_kms = idp
        handler.get_jwks()
        handler.get_jwks()
        handler.current_kid()
        # 缓存生效:KMS GetPublicKey 只该被打一次
        assert fake_kms.get_public_key_calls == 1

    def test_discovery_issuer_matches_env(self, idp):
        handler, _, _ = idp
        doc = body_of(
            handler.lambda_handler(
                make_event("GET", "/.well-known/openid-configuration"), None
            )
        )
        assert doc["issuer"] == handler.ISSUER
        assert doc["jwks_uri"] == f"{handler.ISSUER}/.well-known/jwks.json"
        assert doc["id_token_signing_alg_values_supported"] == ["RS256"]


# ---------------------------------------------------------------------------
# JWT 签名
# ---------------------------------------------------------------------------


class TestJwtSigning:
    def test_signature_verifies_against_published_jwks(self, idp, rsa_key):
        """端到端:handler 签 -> 从 JWKS 取 n/e -> 标准 RS256 验签。"""
        handler, _, _ = idp
        token = handler.sign_jwt({"iss": handler.ISSUER, "sub": "someone"})
        header_b64, payload_b64, signature_b64 = token.split(".")

        jwk = handler.get_jwks()["keys"][0]
        n = int.from_bytes(handler.b64u_decode(jwk["n"]), "big")
        e = int.from_bytes(handler.b64u_decode(jwk["e"]), "big")

        assert rsa_stub.verify_pkcs1v15_sha256(
            n,
            e,
            f"{header_b64}.{payload_b64}".encode(),
            handler.b64u_decode(signature_b64),
        )

    def test_tampered_payload_fails_verification(self, idp):
        handler, _, _ = idp
        token = handler.sign_jwt({"iss": handler.ISSUER, "sub": "alice", "scope": "tools:read"})
        header_b64, _, signature_b64 = token.split(".")
        forged = handler.b64u_encode(
            json.dumps({"iss": handler.ISSUER, "sub": "alice", "scope": "tools:write"}).encode()
        )

        jwk = handler.get_jwks()["keys"][0]
        assert not rsa_stub.verify_pkcs1v15_sha256(
            int.from_bytes(handler.b64u_decode(jwk["n"]), "big"),
            int.from_bytes(handler.b64u_decode(jwk["e"]), "big"),
            f"{header_b64}.{forged}".encode(),
            handler.b64u_decode(signature_b64),
        )

    def test_header_declares_kid_present_in_jwks(self, idp):
        handler, _, _ = idp
        token = handler.sign_jwt({"sub": "x"})
        header = json.loads(handler.b64u_decode(token.split(".")[0]))

        assert header["alg"] == "RS256"
        assert header["typ"] == "JWT"
        assert header["kid"] == handler.get_jwks()["keys"][0]["kid"]

    def test_access_token_claims(self, idp):
        handler, _, _ = idp
        before = int(time.time())
        token, jti, exp = handler.build_access_token(
            subject="actor-1",
            client_id="c1",
            scope="tools:read",
            audience="agentcore-cn",
            extra={"actor_id": "actor-1"},
        )
        claims = json.loads(handler.b64u_decode(token.split(".")[1]))

        assert claims["iss"] == handler.ISSUER
        assert claims["sub"] == "actor-1"
        assert claims["aud"] == "agentcore-cn"
        assert claims["scope"] == "tools:read"
        assert claims["jti"] == jti
        assert claims["token_use"] == "access"
        assert claims["exp"] == exp
        assert before <= claims["iat"] <= exp
        assert exp - claims["iat"] == handler.ACCESS_TOKEN_TTL


# ---------------------------------------------------------------------------
# 密码哈希
# ---------------------------------------------------------------------------


class TestPasswordHashing:
    def test_roundtrip(self, idp):
        handler, _, _ = idp
        stored = handler.hash_password("correct horse battery staple")
        assert handler.verify_password("correct horse battery staple", stored)
        assert not handler.verify_password("wrong password", stored)

    def test_salt_is_random(self, idp):
        handler, _, _ = idp
        assert handler.hash_password("same") != handler.hash_password("same")

    def test_stored_format_carries_its_own_round_count(self, idp):
        handler, _, _ = idp
        stored = handler.hash_password("pw")
        scheme, rounds, salt, digest = stored.split("$")
        assert scheme == "pbkdf2_sha256"
        assert int(rounds) == handler.PBKDF2_ROUNDS
        assert salt and digest

    @pytest.mark.parametrize(
        "malformed",
        ["", "not-a-hash", "pbkdf2_sha256$abc$def", "md5$1$x$y", "pbkdf2_sha256$x$y$z"],
    )
    def test_malformed_hash_is_rejected_not_crashed(self, idp, malformed):
        handler, _, _ = idp
        assert handler.verify_password("anything", malformed) is False


# ---------------------------------------------------------------------------
# token 端点
# ---------------------------------------------------------------------------


class TestClientCredentialsGrant:
    def test_happy_path_with_basic_auth(self, idp):
        handler, _, _ = idp
        client_id, secret = seed_client(idp)

        response = handler.lambda_handler(
            make_event(
                "POST",
                "/oauth2/token",
                form={"grant_type": "client_credentials", "scope": "gateway:invoke"},
                basic_auth=(client_id, secret),
            ),
            None,
        )

        assert response["statusCode"] == 200
        payload = body_of(response)
        assert payload["token_type"] == "Bearer"
        assert payload["scope"] == "gateway:invoke"
        assert payload["expires_in"] > 0
        # M2M 不该发 refresh token
        assert "refresh_token" not in payload
        claims = json.loads(handler.b64u_decode(payload["access_token"].split(".")[1]))
        assert claims["sub"] == f"client:{client_id}"

    def test_happy_path_with_post_body_credentials(self, idp):
        handler, _, _ = idp
        client_id, secret = seed_client(idp)

        response = handler.lambda_handler(
            make_event(
                "POST",
                "/oauth2/token",
                form={
                    "grant_type": "client_credentials",
                    "client_id": client_id,
                    "client_secret": secret,
                },
            ),
            None,
        )
        assert response["statusCode"] == 200

    def test_omitted_scope_grants_everything_the_client_may_have(self, idp):
        handler, _, _ = idp
        client_id, secret = seed_client(idp, scopes=("tools:read", "tools:write"))

        response = handler.lambda_handler(
            make_event(
                "POST",
                "/oauth2/token",
                form={"grant_type": "client_credentials"},
                basic_auth=(client_id, secret),
            ),
            None,
        )
        assert body_of(response)["scope"] == "tools:read tools:write"

    def test_scope_escalation_is_refused(self, idp):
        handler, _, _ = idp
        client_id, secret = seed_client(idp, scopes=("tools:read",))

        response = handler.lambda_handler(
            make_event(
                "POST",
                "/oauth2/token",
                form={"grant_type": "client_credentials", "scope": "tools:write"},
                basic_auth=(client_id, secret),
            ),
            None,
        )
        assert response["statusCode"] == 400
        assert body_of(response)["error"] == "invalid_scope"

    def test_wrong_secret_is_401(self, idp):
        handler, _, _ = idp
        client_id, _ = seed_client(idp)

        response = handler.lambda_handler(
            make_event(
                "POST",
                "/oauth2/token",
                form={"grant_type": "client_credentials"},
                basic_auth=(client_id, "wrong-secret"),
            ),
            None,
        )
        assert response["statusCode"] == 401
        assert body_of(response)["error"] == "invalid_client"

    def test_unknown_client_is_401(self, idp):
        handler, _, _ = idp
        response = handler.lambda_handler(
            make_event(
                "POST",
                "/oauth2/token",
                form={"grant_type": "client_credentials"},
                basic_auth=("ghost", "whatever"),
            ),
            None,
        )
        assert response["statusCode"] == 401

    def test_grant_type_not_enabled_for_client(self, idp):
        handler, _, _ = idp
        client_id, secret = seed_client(idp, grant_types=("password",))

        response = handler.lambda_handler(
            make_event(
                "POST",
                "/oauth2/token",
                form={"grant_type": "client_credentials"},
                basic_auth=(client_id, secret),
            ),
            None,
        )
        assert response["statusCode"] == 400
        assert body_of(response)["error"] == "unauthorized_client"


class TestPasswordGrant:
    def test_happy_path(self, idp):
        handler, _, _ = idp
        client_id, secret = seed_client(idp)
        username, password = seed_user(idp)

        response = handler.lambda_handler(
            make_event(
                "POST",
                "/oauth2/token",
                form={"grant_type": "password", "username": username, "password": password},
                basic_auth=(client_id, secret),
            ),
            None,
        )

        assert response["statusCode"] == 200
        payload = body_of(response)
        assert payload["refresh_token"]
        claims = json.loads(handler.b64u_decode(payload["access_token"].split(".")[1]))
        # actor_id 会被 Agent 用来隔离 MemoryLite 数据,必须带上
        assert claims["actor_id"] == "actor-demo-001"
        assert claims["sub"] == "actor-demo-001"
        assert claims["username"] == username

    def test_scope_is_intersection_of_client_and_user(self, idp):
        handler, _, _ = idp
        client_id, secret = seed_client(idp, scopes=("agent:invoke", "tools:read", "tools:write"))
        username, password = seed_user(idp, scopes=("tools:read", "tools:write", "gateway:invoke"))

        response = handler.lambda_handler(
            make_event(
                "POST",
                "/oauth2/token",
                form={"grant_type": "password", "username": username, "password": password},
                basic_auth=(client_id, secret),
            ),
            None,
        )
        # agent:invoke 用户没有,gateway:invoke 客户端没有,都不该出现
        assert body_of(response)["scope"] == "tools:read tools:write"

    def test_wrong_password(self, idp):
        handler, _, _ = idp
        client_id, secret = seed_client(idp)
        username, _ = seed_user(idp)

        response = handler.lambda_handler(
            make_event(
                "POST",
                "/oauth2/token",
                form={"grant_type": "password", "username": username, "password": "nope"},
                basic_auth=(client_id, secret),
            ),
            None,
        )
        assert response["statusCode"] == 400
        assert body_of(response)["error"] == "invalid_grant"

    def test_unknown_user_gives_same_error_as_wrong_password(self, idp):
        """不能让攻击者靠错误信息区分"用户不存在"和"密码错误"。"""
        handler, _, _ = idp
        client_id, secret = seed_client(idp)
        seed_user(idp)

        unknown = handler.lambda_handler(
            make_event(
                "POST",
                "/oauth2/token",
                form={"grant_type": "password", "username": "ghost", "password": "x"},
                basic_auth=(client_id, secret),
            ),
            None,
        )
        wrong_pw = handler.lambda_handler(
            make_event(
                "POST",
                "/oauth2/token",
                form={"grant_type": "password", "username": "demo-user", "password": "x"},
                basic_auth=(client_id, secret),
            ),
            None,
        )
        assert unknown["statusCode"] == wrong_pw["statusCode"] == 400
        assert body_of(unknown) == body_of(wrong_pw)

    def test_disabled_user_cannot_log_in(self, idp):
        handler, _, _ = idp
        client_id, secret = seed_client(idp)
        username, password = seed_user(idp, disabled=True)

        response = handler.lambda_handler(
            make_event(
                "POST",
                "/oauth2/token",
                form={"grant_type": "password", "username": username, "password": password},
                basic_auth=(client_id, secret),
            ),
            None,
        )
        assert response["statusCode"] == 400
        assert body_of(response)["error"] == "invalid_grant"


class TestRefreshTokenGrant:
    def _login(self, handler, client_id, secret, username, password) -> dict:
        return body_of(
            handler.lambda_handler(
                make_event(
                    "POST",
                    "/oauth2/token",
                    form={"grant_type": "password", "username": username, "password": password},
                    basic_auth=(client_id, secret),
                ),
                None,
            )
        )

    def test_refresh_rotates_and_preserves_scope(self, idp):
        handler, _, _ = idp
        client_id, secret = seed_client(idp)
        username, password = seed_user(idp)
        first = self._login(handler, client_id, secret, username, password)

        response = handler.lambda_handler(
            make_event(
                "POST",
                "/oauth2/token",
                form={"grant_type": "refresh_token", "refresh_token": first["refresh_token"]},
                basic_auth=(client_id, secret),
            ),
            None,
        )

        assert response["statusCode"] == 200
        second = body_of(response)
        assert second["refresh_token"] != first["refresh_token"]
        assert second["scope"] == first["scope"]

    def test_refresh_token_is_single_use(self, idp):
        handler, _, _ = idp
        client_id, secret = seed_client(idp)
        username, password = seed_user(idp)
        first = self._login(handler, client_id, secret, username, password)
        event = make_event(
            "POST",
            "/oauth2/token",
            form={"grant_type": "refresh_token", "refresh_token": first["refresh_token"]},
            basic_auth=(client_id, secret),
        )

        assert handler.lambda_handler(event, None)["statusCode"] == 200
        replayed = handler.lambda_handler(event, None)

        assert replayed["statusCode"] == 400
        assert body_of(replayed)["error"] == "invalid_grant"

    def test_refresh_token_is_bound_to_issuing_client(self, idp):
        handler, _, _ = idp
        client_a, secret_a = seed_client(idp, client_id="client-a")
        client_b, secret_b = seed_client(idp, client_id="client-b")
        username, password = seed_user(idp)
        issued = self._login(handler, client_a, secret_a, username, password)

        response = handler.lambda_handler(
            make_event(
                "POST",
                "/oauth2/token",
                form={"grant_type": "refresh_token", "refresh_token": issued["refresh_token"]},
                basic_auth=(client_b, secret_b),
            ),
            None,
        )
        assert response["statusCode"] == 400
        assert body_of(response)["error"] == "invalid_grant"

    def test_expired_refresh_token_rejected_even_if_ttl_sweep_lagged(self, idp):
        """DynamoDB TTL 删除有延迟,handler 必须自己再判一次过期。"""
        handler, ddb, _ = idp
        client_id, secret = seed_client(idp)
        username, password = seed_user(idp)
        issued = self._login(handler, client_id, secret, username, password)

        key = (f"REFRESH#{handler._hash_token(issued['refresh_token'])}", "TOKEN")
        ddb.items[key]["expires_at"] = {"N": str(int(time.time()) - 1)}

        response = handler.lambda_handler(
            make_event(
                "POST",
                "/oauth2/token",
                form={"grant_type": "refresh_token", "refresh_token": issued["refresh_token"]},
                basic_auth=(client_id, secret),
            ),
            None,
        )
        assert response["statusCode"] == 400

    def test_refresh_token_stored_only_as_hash(self, idp):
        handler, ddb, _ = idp
        client_id, secret = seed_client(idp)
        username, password = seed_user(idp)
        issued = self._login(handler, client_id, secret, username, password)

        serialized = json.dumps(
            {f"{pk}|{sk}": v for (pk, sk), v in ddb.items.items()}, default=str
        )
        assert issued["refresh_token"] not in serialized


class TestIntrospectAndRevoke:
    def _token(self, handler, client_id, secret) -> str:
        return body_of(
            handler.lambda_handler(
                make_event(
                    "POST",
                    "/oauth2/token",
                    form={"grant_type": "client_credentials"},
                    basic_auth=(client_id, secret),
                ),
                None,
            )
        )["access_token"]

    def test_active_token(self, idp):
        handler, _, _ = idp
        client_id, secret = seed_client(idp)
        token = self._token(handler, client_id, secret)

        response = handler.lambda_handler(
            make_event(
                "POST",
                "/oauth2/introspect",
                form={"token": token},
                basic_auth=(client_id, secret),
            ),
            None,
        )
        payload = body_of(response)
        assert payload["active"] is True
        assert payload["client_id"] == client_id

    def test_revoked_token_becomes_inactive(self, idp):
        handler, _, _ = idp
        client_id, secret = seed_client(idp)
        token = self._token(handler, client_id, secret)

        revoke = handler.lambda_handler(
            make_event(
                "POST", "/oauth2/revoke", form={"token": token}, basic_auth=(client_id, secret)
            ),
            None,
        )
        assert revoke["statusCode"] == 200

        after = body_of(
            handler.lambda_handler(
                make_event(
                    "POST",
                    "/oauth2/introspect",
                    form={"token": token},
                    basic_auth=(client_id, secret),
                ),
                None,
            )
        )
        assert after["active"] is False

    def test_expired_token_is_inactive(self, idp, monkeypatch):
        handler, _, _ = idp
        client_id, secret = seed_client(idp)
        monkeypatch.setattr(handler, "ACCESS_TOKEN_TTL", -10)
        token = self._token(handler, client_id, secret)

        payload = body_of(
            handler.lambda_handler(
                make_event(
                    "POST",
                    "/oauth2/introspect",
                    form={"token": token},
                    basic_auth=(client_id, secret),
                ),
                None,
            )
        )
        assert payload["active"] is False

    def test_garbage_token_is_inactive_not_an_error(self, idp):
        handler, _, _ = idp
        client_id, secret = seed_client(idp)

        payload = body_of(
            handler.lambda_handler(
                make_event(
                    "POST",
                    "/oauth2/introspect",
                    form={"token": "not.a.jwt"},
                    basic_auth=(client_id, secret),
                ),
                None,
            )
        )
        assert payload == {"active": False}

    def test_forged_payload_is_rejected(self, idp):
        """introspect 必须验签。否则任何持有 client 凭证的调用方
        都能伪造 claims 换来一个 active:true,等于提权。"""
        handler, _, _ = idp
        client_id, secret = seed_client(idp)
        token = self._token(handler, client_id, secret)
        header_b64, payload_b64, signature_b64 = token.split(".")

        claims = json.loads(handler.b64u_decode(payload_b64))
        claims["scope"] = "tools:write gateway:invoke"
        claims["sub"] = "actor-admin"
        forged = ".".join(
            (
                header_b64,
                handler.b64u_encode(json.dumps(claims).encode()),
                signature_b64,
            )
        )

        payload = body_of(
            handler.lambda_handler(
                make_event(
                    "POST",
                    "/oauth2/introspect",
                    form={"token": forged},
                    basic_auth=(client_id, secret),
                ),
                None,
            )
        )
        assert payload == {"active": False}

    def test_alg_none_downgrade_is_rejected(self, idp):
        """经典的 alg:none 攻击:去掉签名、把 alg 改成 none。"""
        handler, _, _ = idp
        client_id, secret = seed_client(idp)
        token = self._token(handler, client_id, secret)
        _, payload_b64, _ = token.split(".")

        forged_header = handler.b64u_encode(
            json.dumps({"alg": "none", "typ": "JWT", "kid": handler.current_kid()}).encode()
        )

        payload = body_of(
            handler.lambda_handler(
                make_event(
                    "POST",
                    "/oauth2/introspect",
                    form={"token": f"{forged_header}.{payload_b64}."},
                    basic_auth=(client_id, secret),
                ),
                None,
            )
        )
        assert payload == {"active": False}

    def test_token_signed_by_another_key_is_rejected(self, idp):
        handler, _, fake_kms = idp
        client_id, secret = seed_client(idp)
        token = self._token(handler, client_id, secret)
        header_b64, payload_b64, _ = token.split(".")

        attacker_key = rsa_stub.generate_key(1024)
        rogue_signature = handler.b64u_encode(
            rsa_stub.sign_pkcs1v15_sha256(attacker_key, f"{header_b64}.{payload_b64}".encode())
        )

        payload = body_of(
            handler.lambda_handler(
                make_event(
                    "POST",
                    "/oauth2/introspect",
                    form={"token": f"{header_b64}.{payload_b64}.{rogue_signature}"},
                    basic_auth=(client_id, secret),
                ),
                None,
            )
        )
        assert payload == {"active": False}

    def test_wrong_issuer_is_rejected(self, idp, monkeypatch):
        handler, _, _ = idp
        client_id, secret = seed_client(idp)
        # 用另一个 issuer 签,再用本 IdP 的配置去校验
        monkeypatch.setattr(handler, "ISSUER", "https://evil.example.test")
        token = self._token(handler, client_id, secret)
        monkeypatch.setattr(handler, "ISSUER", "https://idp.example.test")

        payload = body_of(
            handler.lambda_handler(
                make_event(
                    "POST",
                    "/oauth2/introspect",
                    form={"token": token},
                    basic_auth=(client_id, secret),
                ),
                None,
            )
        )
        assert payload == {"active": False}

    def test_introspect_requires_client_auth(self, idp):
        handler, _, _ = idp
        seed_client(idp)
        response = handler.lambda_handler(
            make_event("POST", "/oauth2/introspect", form={"token": "x"}), None
        )
        assert response["statusCode"] == 401

    def test_revoke_unknown_token_still_returns_200(self, idp):
        """RFC 7009 要求的行为,不能靠返回码泄露 token 是否存在。"""
        handler, _, _ = idp
        client_id, secret = seed_client(idp)
        response = handler.lambda_handler(
            make_event(
                "POST",
                "/oauth2/revoke",
                form={"token": "totally-unknown"},
                basic_auth=(client_id, secret),
            ),
            None,
        )
        assert response["statusCode"] == 200


# ---------------------------------------------------------------------------
# 路由与请求解析
# ---------------------------------------------------------------------------


class TestRoutingAndParsing:
    def test_unknown_route_is_404(self, idp):
        handler, _, _ = idp
        response = handler.lambda_handler(make_event("GET", "/nope"), None)
        assert response["statusCode"] == 404

    def test_authorize_endpoint_declines_cleanly(self, idp):
        handler, _, _ = idp
        response = handler.lambda_handler(make_event("GET", "/oauth2/authorize"), None)
        assert response["statusCode"] == 400
        assert body_of(response)["error"] == "unsupported_response_type"

    def test_base64_encoded_body_is_decoded(self, idp):
        handler, _, _ = idp
        client_id, secret = seed_client(idp)
        response = handler.lambda_handler(
            make_event(
                "POST",
                "/oauth2/token",
                form={"grant_type": "client_credentials"},
                basic_auth=(client_id, secret),
                base64_encode=True,
            ),
            None,
        )
        assert response["statusCode"] == 200

    def test_json_body_is_accepted(self, idp):
        handler, _, _ = idp
        client_id, secret = seed_client(idp)
        response = handler.lambda_handler(
            make_event(
                "POST",
                "/oauth2/token",
                json_body={
                    "grant_type": "client_credentials",
                    "client_id": client_id,
                    "client_secret": secret,
                },
            ),
            None,
        )
        assert response["statusCode"] == 200

    def test_credentials_with_special_chars_survive_basic_auth(self, idp):
        """RFC 6749 §2.3.1 要求 Basic 里的值先做 form-urlencode。"""
        handler, _, _ = idp
        client_id, secret = seed_client(idp, secret="p@ss:word/with+chars=")
        response = handler.lambda_handler(
            make_event(
                "POST",
                "/oauth2/token",
                form={"grant_type": "client_credentials"},
                basic_auth=(client_id, secret),
            ),
            None,
        )
        assert response["statusCode"] == 200

    def test_grant_type_absent_from_client_allowlist(self, idp):
        handler, _, _ = idp
        client_id, secret = seed_client(idp)
        response = handler.lambda_handler(
            make_event(
                "POST",
                "/oauth2/token",
                form={"grant_type": "authorization_code"},
                basic_auth=(client_id, secret),
            ),
            None,
        )
        assert response["statusCode"] == 400
        assert body_of(response)["error"] == "unauthorized_client"

    def test_grant_type_allowed_but_not_implemented(self, idp):
        """客户端记录里写了个 handler 没实现的 grant,要落到 unsupported_grant_type。"""
        handler, _, _ = idp
        client_id, secret = seed_client(idp, grant_types=("device_code",))
        response = handler.lambda_handler(
            make_event(
                "POST",
                "/oauth2/token",
                form={"grant_type": "device_code"},
                basic_auth=(client_id, secret),
            ),
            None,
        )
        assert response["statusCode"] == 400
        assert body_of(response)["error"] == "unsupported_grant_type"

    def test_internal_errors_do_not_leak_details(self, idp, monkeypatch):
        handler, _, _ = idp

        def boom(_event):
            raise RuntimeError("secret internal detail: db password is hunter2")

        monkeypatch.setitem(handler.ROUTES, ("POST", "/oauth2/token"), boom)
        response = handler.lambda_handler(make_event("POST", "/oauth2/token", form={}), None)

        assert response["statusCode"] == 500
        assert "hunter2" not in response["body"]
        assert body_of(response)["error"] == "server_error"

    def test_token_responses_are_not_cacheable(self, idp):
        handler, _, _ = idp
        client_id, secret = seed_client(idp)
        response = handler.lambda_handler(
            make_event(
                "POST",
                "/oauth2/token",
                form={"grant_type": "client_credentials"},
                basic_auth=(client_id, secret),
            ),
            None,
        )
        assert response["headers"]["Cache-Control"] == "no-store"

    def test_jwks_is_cacheable(self, idp):
        handler, _, _ = idp
        response = handler.lambda_handler(make_event("GET", "/.well-known/jwks.json"), None)
        assert "max-age" in response["headers"]["Cache-Control"]
