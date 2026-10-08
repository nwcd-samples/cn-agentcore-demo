"""Demo 级 OAuth 2.1 / DCR / PKCE 的协议与攻击面测试。"""

from __future__ import annotations

import base64
import hashlib
import json
import time
import urllib.parse

import pytest

RESOURCE = "https://gateway.example.test/mcp"
QUICK_REDIRECT = "https://us-east-1.quicksight.aws.amazon.com/sn/oauthcallback"


def event(method, path, *, form=None, json_body=None, query=None):
    headers = {}
    body = ""
    if form is not None:
        headers["content-type"] = "application/x-www-form-urlencoded"
        body = urllib.parse.urlencode(form)
    elif json_body is not None:
        headers["content-type"] = "application/json"
        body = json.dumps(json_body)
    result = {
        "requestContext": {"http": {"method": method, "path": path}},
        "headers": headers,
        "body": body,
        "isBase64Encoded": False,
    }
    if query is not None:
        result["queryStringParameters"] = query
        result["rawQueryString"] = urllib.parse.urlencode(query)
    return result


def json_body(response):
    return json.loads(response["body"])


def seed_user(handler, ddb, *, username="demo-user", password="demo-password"):
    ddb.put_item(
        TableName="t",
        Item={
            "PK": {"S": f"USER#{username}"},
            "SK": {"S": "PROFILE"},
            "password_hash": {"S": handler.hash_password(password)},
            "scopes": {"SS": ["agent:invoke", "tools:read", "tools:write"]},
            "actor_id": {"S": f"actor-{username}"},
            "disabled": {"BOOL": False},
        },
    )
    return username, password


def register(handler, *, redirect=QUICK_REDIRECT, auth_method="none", **overrides):
    payload = {
        "client_name": "Amazon Quick",
        "redirect_uris": [redirect],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": auth_method,
        **overrides,
    }
    return handler.lambda_handler(
        event("POST", "/oauth2/register", json_body=payload), None
    )


def pkce_pair():
    verifier = "v" * 64
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return verifier, challenge


def authorization_params(client_id, challenge, *, redirect=QUICK_REDIRECT, **overrides):
    return {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect,
        "scope": "gateway:invoke tools:read tools:write",
        "state": "quick-state-123",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "resource": RESOURCE,
        **overrides,
    }


def issue_code(handler, ddb, *, auth_method="none"):
    username, password = seed_user(handler, ddb)
    registered = json_body(register(handler, auth_method=auth_method))
    verifier, challenge = pkce_pair()
    params = authorization_params(registered["client_id"], challenge)
    response = handler.lambda_handler(
        event(
            "POST",
            "/oauth2/authorize",
            form={**params, "username": username, "password": password},
        ),
        None,
    )
    assert response["statusCode"] == 302
    location = urllib.parse.urlsplit(response["headers"]["Location"])
    query = urllib.parse.parse_qs(location.query)
    assert query["state"] == [params["state"]]
    return registered, verifier, query["code"][0], params


@pytest.fixture(autouse=True)
def oauth_config(idp, monkeypatch):
    handler, _, _ = idp
    monkeypatch.setattr(handler, "MCP_RESOURCE", RESOURCE)
    monkeypatch.setattr(handler, "AUTH_CODE_TTL", 300)
    monkeypatch.setattr(handler, "DCR_CLIENT_TTL", 3600)
    monkeypatch.setattr(handler, "DCR_ALLOWED_REDIRECT_HOSTS", {"quicksight.aws.amazon.com"})


class TestDiscovery:
    def test_advertises_dcr_authorization_code_and_pkce(self, idp):
        handler, _, _ = idp
        response = handler.lambda_handler(
            event("GET", "/.well-known/oauth-authorization-server"), None
        )
        metadata = json_body(response)
        assert metadata["registration_endpoint"] == f"{handler.ISSUER}/oauth2/register"
        assert "authorization_code" in metadata["grant_types_supported"]
        assert metadata["response_types_supported"] == ["code"]
        assert metadata["code_challenge_methods_supported"] == ["S256"]
        assert "none" in metadata["token_endpoint_auth_methods_supported"]


class TestDynamicClientRegistration:
    def test_registers_public_authorization_code_client(self, idp):
        handler, ddb, _ = idp
        response = register(handler)
        assert response["statusCode"] == 201
        payload = json_body(response)
        assert payload["client_id"].startswith("dcr-")
        assert payload["token_endpoint_auth_method"] == "none"
        assert "client_secret" not in payload
        stored = ddb.items[(f"CLIENT#{payload['client_id']}", "PROFILE")]
        assert stored["public_client"] == {"BOOL": True}
        assert stored["dynamic"] == {"BOOL": True}
        assert stored["redirect_uris"]["SS"] == [QUICK_REDIRECT]
        assert stored["audience"] == {"S": RESOURCE}
        assert "secret_hash" not in stored

    def test_confidential_registration_returns_secret_but_stores_only_hash(self, idp):
        handler, ddb, _ = idp
        response = register(handler, auth_method="client_secret_basic")
        payload = json_body(response)
        assert response["statusCode"] == 201
        assert len(payload["client_secret"]) >= 32
        stored = ddb.items[(f"CLIENT#{payload['client_id']}", "PROFILE")]
        assert payload["client_secret"] not in json.dumps(stored)
        assert handler.verify_password(payload["client_secret"], stored["secret_hash"]["S"])

    @pytest.mark.parametrize(
        "redirect",
        [
            "https://evil.example/callback",
            "http://us-east-1.quicksight.aws.amazon.com/callback",
            "https://quicksight.aws.amazon.com/callback#fragment",
            "javascript:alert(1)",
        ],
    )
    def test_rejects_untrusted_redirects(self, idp, redirect):
        handler, _, _ = idp
        response = register(handler, redirect=redirect)
        assert response["statusCode"] == 400
        assert json_body(response)["error"] == "invalid_redirect_uri"

    @pytest.mark.parametrize("redirect", ["http://localhost:4567/callback", "http://127.0.0.1:7890/oauth/callback"])
    def test_allows_native_loopback_redirects(self, idp, redirect):
        handler, _, _ = idp
        assert register(handler, redirect=redirect)["statusCode"] == 201

    def test_never_allows_dynamic_client_credentials(self, idp):
        handler, _, _ = idp
        response = register(
            handler,
            grant_types=["client_credentials"],
            response_types=[],
        )
        assert response["statusCode"] == 400
        assert json_body(response)["error"] == "invalid_client_metadata"

    def test_expired_dynamic_client_is_not_loaded(self, idp):
        handler, ddb, _ = idp
        payload = json_body(register(handler))
        stored = ddb.items[(f"CLIENT#{payload['client_id']}", "PROFILE")]
        stored["expires_at"] = {"N": str(int(time.time()) - 1)}
        assert handler.load_client(payload["client_id"]) is None


class TestAuthorizationEndpoint:
    def test_get_renders_login_and_security_headers(self, idp):
        handler, _, _ = idp
        registered = json_body(register(handler))
        _, challenge = pkce_pair()
        response = handler.lambda_handler(
            event(
                "GET",
                "/oauth2/authorize",
                query=authorization_params(registered["client_id"], challenge),
            ),
            None,
        )
        assert response["statusCode"] == 200
        assert response["headers"]["Content-Type"].startswith("text/html")
        assert "base-uri 'none'" in response["headers"]["Content-Security-Policy"]
        assert "frame-ancestors 'none'" in response["headers"]["Content-Security-Policy"]
        assert 'type="password"' in response["body"]
        assert "Amazon Quick" in response["body"]

    def test_login_redirects_with_code_and_original_state(self, idp):
        handler, ddb, _ = idp
        _, _, _, params = issue_code(handler, ddb)
        # issue_code 已断言302；确认数据库里只存code哈希，且state不进code记录。
        code_records = [item for (pk, sk), item in ddb.items.items() if pk.startswith("AUTHCODE#") and sk == "CODE"]
        assert len(code_records) == 1
        assert code_records[0]["redirect_uri"] == {"S": params["redirect_uri"]}
        assert "state" not in code_records[0]

    def test_wrong_password_does_not_issue_code(self, idp):
        handler, ddb, _ = idp
        username, _ = seed_user(handler, ddb)
        registered = json_body(register(handler))
        _, challenge = pkce_pair()
        params = authorization_params(registered["client_id"], challenge)
        response = handler.lambda_handler(
            event("POST", "/oauth2/authorize", form={**params, "username": username, "password": "wrong"}),
            None,
        )
        assert response["statusCode"] == 200
        assert "用户名或密码错误" in response["body"]
        assert not any(pk.startswith("AUTHCODE#") for pk, _ in ddb.items)

    def test_rejects_redirect_resource_and_plain_pkce_tampering(self, idp):
        handler, _, _ = idp
        registered = json_body(register(handler))
        _, challenge = pkce_pair()
        base = authorization_params(registered["client_id"], challenge)
        cases = [
            {**base, "redirect_uri": "https://evil.example/callback"},
            {**base, "resource": "https://other.example/mcp"},
            {**base, "code_challenge_method": "plain"},
        ]
        for params in cases:
            assert handler.lambda_handler(event("GET", "/oauth2/authorize", query=params), None)["statusCode"] == 400


class TestAuthorizationCodeToken:
    def test_code_exchange_issues_user_token_bound_to_resource(self, idp):
        handler, ddb, _ = idp
        registered, verifier, code, params = issue_code(handler, ddb)
        response = handler.lambda_handler(
            event(
                "POST",
                "/oauth2/token",
                form={
                    "grant_type": "authorization_code",
                    "client_id": registered["client_id"],
                    "code": code,
                    "redirect_uri": params["redirect_uri"],
                    "code_verifier": verifier,
                    "resource": RESOURCE,
                },
            ),
            None,
        )
        assert response["statusCode"] == 200
        payload = json_body(response)
        assert payload["refresh_token"]
        claims = json.loads(handler.b64u_decode(payload["access_token"].split(".")[1]))
        assert claims["actor_id"] == "actor-demo-user"
        assert claims["client_id"] == registered["client_id"]
        assert claims["resource"] == RESOURCE
        assert claims["auth_flow"] == "authorization_code"
        assert claims["aud"] == RESOURCE
        assert set(claims["scope"].split()) == {
            "gateway:invoke",
            "tools:read",
            "tools:write",
        }

    def test_code_is_single_use(self, idp):
        handler, ddb, _ = idp
        registered, verifier, code, params = issue_code(handler, ddb)
        form = {
            "grant_type": "authorization_code",
            "client_id": registered["client_id"],
            "code": code,
            "redirect_uri": params["redirect_uri"],
            "code_verifier": verifier,
        }
        assert handler.lambda_handler(event("POST", "/oauth2/token", form=form), None)["statusCode"] == 200
        replay = handler.lambda_handler(event("POST", "/oauth2/token", form=form), None)
        assert replay["statusCode"] == 400
        assert json_body(replay)["error"] == "invalid_grant"

    def test_wrong_verifier_redirect_or_client_is_rejected(self, idp):
        handler, ddb, _ = idp
        for mutation in ("verifier", "redirect", "client"):
            registered, verifier, code, params = issue_code(handler, ddb)
            form = {
                "grant_type": "authorization_code",
                "client_id": registered["client_id"],
                "code": code,
                "redirect_uri": params["redirect_uri"],
                "code_verifier": verifier,
            }
            if mutation == "verifier":
                form["code_verifier"] = "x" * 64
            elif mutation == "redirect":
                form["redirect_uri"] = "https://evil.example/callback"
            else:
                other = json_body(register(handler))
                form["client_id"] = other["client_id"]
            response = handler.lambda_handler(event("POST", "/oauth2/token", form=form), None)
            assert response["statusCode"] == 400
            assert json_body(response)["error"] in {"invalid_grant", "unauthorized_client"}

    def test_wrong_binding_does_not_consume_authorization_code(self, idp):
        handler, ddb, _ = idp
        registered, verifier, code, params = issue_code(handler, ddb)
        base = {
            "grant_type": "authorization_code",
            "client_id": registered["client_id"],
            "code": code,
            "redirect_uri": params["redirect_uri"],
            "code_verifier": verifier,
        }
        wrong = handler.lambda_handler(
            event("POST", "/oauth2/token", form={**base, "code_verifier": "x" * 64}),
            None,
        )
        assert wrong["statusCode"] == 400
        assert handler.lambda_handler(event("POST", "/oauth2/token", form=base), None)["statusCode"] == 200

    def test_registered_client_auth_method_is_enforced(self, idp):
        handler, ddb, _ = idp
        registered, verifier, code, params = issue_code(
            handler, ddb, auth_method="client_secret_basic"
        )
        form = {
            "grant_type": "authorization_code",
            "client_id": registered["client_id"],
            "client_secret": registered["client_secret"],
            "code": code,
            "redirect_uri": params["redirect_uri"],
            "code_verifier": verifier,
        }
        wrong_method = handler.lambda_handler(event("POST", "/oauth2/token", form=form), None)
        assert wrong_method["statusCode"] == 401
        del form["client_secret"]
        request = event("POST", "/oauth2/token", form=form)
        basic = base64.b64encode(
            f"{registered['client_id']}:{registered['client_secret']}".encode()
        ).decode()
        request["headers"]["Authorization"] = "Basic " + basic
        assert handler.lambda_handler(request, None)["statusCode"] == 200

    def test_public_client_rejects_even_empty_basic_auth(self, idp):
        handler, ddb, _ = idp
        registered, verifier, code, params = issue_code(handler, ddb)
        request = event(
            "POST",
            "/oauth2/token",
            form={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": params["redirect_uri"],
                "code_verifier": verifier,
            },
        )
        request["headers"]["Authorization"] = "Basic " + base64.b64encode(
            f"{registered['client_id']}:".encode()
        ).decode()
        assert handler.lambda_handler(request, None)["statusCode"] == 401

    def test_public_dynamic_client_cannot_use_client_credentials(self, idp):
        handler, _, _ = idp
        registered = json_body(register(handler))
        response = handler.lambda_handler(
            event(
                "POST",
                "/oauth2/token",
                form={"grant_type": "client_credentials", "client_id": registered["client_id"]},
            ),
            None,
        )
        assert response["statusCode"] in {400, 401}

    def test_public_client_can_rotate_refresh_token(self, idp):
        handler, ddb, _ = idp
        registered, verifier, code, params = issue_code(handler, ddb)
        issued = json_body(
            handler.lambda_handler(
                event(
                    "POST",
                    "/oauth2/token",
                    form={
                        "grant_type": "authorization_code",
                        "client_id": registered["client_id"],
                        "code": code,
                        "redirect_uri": params["redirect_uri"],
                        "code_verifier": verifier,
                    },
                ),
                None,
            )
        )
        refreshed = handler.lambda_handler(
            event(
                "POST",
                "/oauth2/token",
                form={
                    "grant_type": "refresh_token",
                    "client_id": registered["client_id"],
                    "refresh_token": issued["refresh_token"],
                },
            ),
            None,
        )
        assert refreshed["statusCode"] == 200
        assert json_body(refreshed)["refresh_token"] != issued["refresh_token"]

    def test_wrong_client_does_not_consume_refresh_token(self, idp):
        handler, ddb, _ = idp
        registered, verifier, code, params = issue_code(handler, ddb)
        issued = json_body(
            handler.lambda_handler(
                event(
                    "POST",
                    "/oauth2/token",
                    form={
                        "grant_type": "authorization_code",
                        "client_id": registered["client_id"],
                        "code": code,
                        "redirect_uri": params["redirect_uri"],
                        "code_verifier": verifier,
                    },
                ),
                None,
            )
        )
        other = json_body(register(handler))
        wrong = handler.lambda_handler(
            event(
                "POST",
                "/oauth2/token",
                form={
                    "grant_type": "refresh_token",
                    "client_id": other["client_id"],
                    "refresh_token": issued["refresh_token"],
                },
            ),
            None,
        )
        assert wrong["statusCode"] == 400
        correct = handler.lambda_handler(
            event(
                "POST",
                "/oauth2/token",
                form={
                    "grant_type": "refresh_token",
                    "client_id": registered["client_id"],
                    "refresh_token": issued["refresh_token"],
                },
            ),
            None,
        )
        assert correct["statusCode"] == 200

    def test_wrong_resource_does_not_consume_refresh_token(self, idp):
        handler, ddb, _ = idp
        registered, verifier, code, params = issue_code(handler, ddb)
        issued = json_body(
            handler.lambda_handler(
                event(
                    "POST",
                    "/oauth2/token",
                    form={
                        "grant_type": "authorization_code",
                        "client_id": registered["client_id"],
                        "code": code,
                        "redirect_uri": params["redirect_uri"],
                        "code_verifier": verifier,
                        "resource": RESOURCE,
                    },
                ),
                None,
            )
        )
        base = {
            "grant_type": "refresh_token",
            "client_id": registered["client_id"],
            "refresh_token": issued["refresh_token"],
        }
        wrong = handler.lambda_handler(
            event("POST", "/oauth2/token", form={**base, "resource": "https://wrong.example/mcp"}),
            None,
        )
        assert wrong["statusCode"] == 400
        assert json_body(wrong)["error"] == "invalid_target"
        correct = handler.lambda_handler(
            event("POST", "/oauth2/token", form={**base, "resource": RESOURCE}),
            None,
        )
        assert correct["statusCode"] == 200

    def test_confidential_dynamic_client_authenticates_code_exchange(self, idp):
        handler, ddb, _ = idp
        registered, verifier, code, params = issue_code(
            handler, ddb, auth_method="client_secret_basic"
        )
        request = event(
            "POST",
            "/oauth2/token",
            form={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": params["redirect_uri"],
                "code_verifier": verifier,
            },
        )
        basic = base64.b64encode(
            f"{registered['client_id']}:{registered['client_secret']}".encode()
        ).decode()
        request["headers"]["Authorization"] = "Basic " + basic
        assert handler.lambda_handler(request, None)["statusCode"] == 200


class TestInfrastructureWiring:
    def test_cloudformation_exposes_all_oauth21_routes(self):
        from pathlib import Path

        template = (Path(__file__).resolve().parents[1] / "infra" / "10-auth-idp.yaml").read_text()
        for route in (
            "POST /oauth2/register",
            "GET /oauth2/authorize",
            "POST /oauth2/authorize",
            "POST /oauth2/token",
        ):
            assert route in template
        assert "AUTH_CODE_TTL" in template
        assert "DCR_CLIENT_TTL" in template
        assert "DCR_ALLOWED_REDIRECT_HOSTS" in template
        assert "MCP_RESOURCE" in template

    def test_quick_oauth_deploy_orders_idp_before_gateway(self):
        from pathlib import Path

        source = (Path(__file__).resolve().parents[1] / "scripts" / "deploy.sh").read_text()
        body = source.split("quick_oauth() {", 1)[1].split("\n}", 1)[0]
        expected = [
            "deploy_idp_stack",
            "push_lambda_code",
            "backfill_issuer",
            "configure_idp_mcp_resource",
            "ENABLE_OAUTH_DCR=1",
            "scripts/create_gateway.py",
        ]
        positions = [body.index(item) for item in expected]
        assert positions == sorted(positions)
        assert 'preflight; quick_oauth ;;' in source

    def test_full_redeploy_restores_mcp_resource_when_dcr_enabled(self):
        from pathlib import Path

        source = (Path(__file__).resolve().parents[1] / "scripts" / "deploy.sh").read_text()
        full = source.split("    all)", 1)[1].split("    *)", 1)[0]
        assert "deploy_stacks" in full
        assert 'ENABLE_OAUTH_DCR:-0' in full
        assert "configure_idp_mcp_resource" in full
        assert full.index("deploy_stacks") < full.index("configure_idp_mcp_resource") < full.index("gateway")

    def test_oauth_verifier_never_prints_credentials_or_tokens(self):
        from pathlib import Path

        source = (Path(__file__).resolve().parents[1] / "scripts" / "verify_quick_oauth.py").read_text()
        assert "print(password" not in source
        assert "print(token" not in source
        assert "print(code" not in source
        assert "access_token=" not in source
        assert "client_secret=" not in source
