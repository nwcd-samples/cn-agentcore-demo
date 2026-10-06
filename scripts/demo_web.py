#!/usr/bin/env python3
"""AgentCore demo 的安全本地 Web 演示后端。

浏览器只连接 127.0.0.1；AWS 凭证、IdP 客户端密钥和 JWT 始终留在
Python 进程内。后端负责：

* 从自建 IdP 换取短期 JWT（带过期缓存）；
* 调用 AgentCore Runtime，并把 Runtime SSE 原样转发给页面；
* 运行完整 selftest，并把结构化结果交给页面展示；
* 只读列出业务表中的订单、运单和工单（显式字段白名单）；
* 提供不含任何敏感配置的 /api/config。

启动：
    .venv/bin/python scripts/demo_web.py
    .venv/bin/python scripts/demo_web.py --no-browser --port 8765
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import threading
import time
import traceback
import uuid
import webbrowser
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError

REPO_ROOT = Path(__file__).resolve().parents[1]
STATIC_FILE = REPO_ROOT / "web" / "demo.html"
MAX_REQUEST_BYTES = 16 * 1024
MAX_PROMPT_CHARS = 4_000
SESSION_RE = re.compile(r"^[A-Za-z0-9._:-]{33,100}$")
QUALIFIER_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}

DEFAULT_PROMPT = (
    "订单 ORD-1024 为什么还没到？请查询真实物流状态并按赔付政策计算金额。"
    "如果确认超时，直接为我创建物流延迟工单。以后此类进展请用短信通知我。"
)

CAPABILITIES = [
    {"name": "Runtime", "kind": "AgentCore", "note": "运行、流式、异步与版本"},
    {"name": "Gateway", "kind": "AgentCore", "note": "受控企业工具"},
    {"name": "Identity", "kind": "AgentCore", "note": "出向凭证"},
    {"name": "Browser", "kind": "AgentCore", "note": "物流网页操作"},
    {"name": "Code Interpreter", "kind": "AgentCore", "note": "精确赔付计算"},
    {"name": "Observability", "kind": "AgentCore", "note": "Trace 与审计"},
    {"name": "OIDC IdP", "kind": "中国区自建", "note": "CUSTOM_JWT 入向"},
    {"name": "MemoryLite", "kind": "中国区自建", "note": "会话与长期偏好"},
]


class DemoError(RuntimeError):
    """可安全展示给本地页面的预期错误。"""


def load_dotenv(path: Path, *, override: bool = False) -> None:
    """加载本项目简单的 KEY=VALUE .env，不执行其中任何 shell 内容。"""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if override or key not in os.environ:
            os.environ[key] = value


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise DemoError(f".env 缺少 {name}")
    return value


@dataclass(frozen=True)
class AppConfig:
    profile: str | None
    region: str
    project: str
    runtime_arn: str
    username: str
    password: str
    client_id: str
    client_secret: str
    token_endpoint: str
    default_qualifier: str = "stable"

    @classmethod
    def from_env(cls) -> "AppConfig":
        region = os.environ.get("AWS_REGION", "cn-northwest-1").strip()
        project = os.environ.get("PROJECT", "agentcore-cn").strip()
        qualifier = os.environ.get("DEMO_RUNTIME_QUALIFIER", "stable").strip()
        if not QUALIFIER_RE.fullmatch(qualifier):
            raise DemoError("DEMO_RUNTIME_QUALIFIER 格式不合法")
        return cls(
            profile=os.environ.get("AWS_PROFILE", "").strip() or None,
            region=region,
            project=project,
            runtime_arn=_required("AGENT_RUNTIME_ARN"),
            username=_required("DEMO_USERNAME"),
            password=_required("DEMO_PASSWORD"),
            client_id=_required("DEMO_CLIENT_ID"),
            client_secret=_required("DEMO_CLIENT_SECRET"),
            token_endpoint=os.environ.get("IDP_TOKEN_ENDPOINT", "").strip(),
            default_qualifier=qualifier,
        )

    @property
    def runtime_id(self) -> str:
        return self.runtime_arn.rsplit("/", 1)[-1]

    def public_dict(self) -> dict[str, Any]:
        """只能返回明确列举的非敏感字段。"""
        return {
            "project": self.project,
            "region": self.region,
            "runtimeId": self.runtime_id,
            "qualifier": self.default_qualifier,
            "defaultPrompt": DEFAULT_PROMPT,
            "capabilities": CAPABILITIES,
            "selftestWritesData": True,
        }


def new_session_id() -> str:
    return f"web-{uuid.uuid4().hex}"


def validate_session_id(value: Any) -> str:
    session_id = str(value or "").strip()
    if not session_id:
        return new_session_id()
    if not SESSION_RE.fullmatch(session_id):
        raise DemoError("sessionId 格式不合法")
    return session_id


def validate_qualifier(value: Any, default: str) -> str:
    qualifier = str(value or default).strip()
    if not QUALIFIER_RE.fullmatch(qualifier):
        raise DemoError("Runtime endpoint 格式不合法")
    return qualifier


def validate_prompt(value: Any) -> str:
    prompt = str(value or "").strip()
    if not prompt:
        raise DemoError("请输入客户问题")
    if len(prompt) > MAX_PROMPT_CHARS:
        raise DemoError(f"问题不能超过 {MAX_PROMPT_CHARS} 个字符")
    return prompt


class DemoService:
    def __init__(self, config: AppConfig):
        self.config = config
        self._token = ""
        self._token_expires_at = 0.0
        self._token_lock = threading.Lock()
        self._endpoint_lock = threading.Lock()
        self._session = boto3.Session(
            profile_name=config.profile,
            region_name=config.region,
        )

    def _discover_token_endpoint(self) -> str:
        if self.config.token_endpoint:
            return self.config.token_endpoint
        with self._endpoint_lock:
            if self.config.token_endpoint:
                return self.config.token_endpoint
            try:
                client = self._session.client(
                    "cloudformation",
                    config=BotoConfig(connect_timeout=10, read_timeout=20),
                )
                response = client.describe_stacks(
                    StackName=f"{self.config.project}-auth-idp"
                )
                outputs = response["Stacks"][0].get("Outputs", [])
                endpoint = next(
                    (
                        item.get("OutputValue", "")
                        for item in outputs
                        if item.get("OutputKey") == "TokenEndpoint"
                    ),
                    "",
                )
            except (BotoCoreError, ClientError, KeyError, IndexError) as exc:
                raise DemoError(
                    "无法发现 IdP 地址；请先完成 AWS SSO 登录，或在 .env 配置 "
                    "IDP_TOKEN_ENDPOINT"
                ) from exc
            if not endpoint.startswith("https://"):
                raise DemoError("CloudFormation 未返回有效的 IdP TokenEndpoint")
            # frozen dataclass 不缓存字段；环境级发现成本只在 token 刷新时发生。
            return endpoint

    def get_token(self, *, force: bool = False) -> str:
        now = time.time()
        if not force and self._token and now < self._token_expires_at - 60:
            return self._token
        with self._token_lock:
            now = time.time()
            if not force and self._token and now < self._token_expires_at - 60:
                return self._token
            endpoint = self._discover_token_endpoint()
            form = urlencode(
                {
                    "grant_type": "password",
                    "username": self.config.username,
                    "password": self.config.password,
                }
            ).encode("utf-8")
            credentials = base64.b64encode(
                f"{self.config.client_id}:{self.config.client_secret}".encode("utf-8")
            ).decode("ascii")
            request = Request(
                endpoint,
                data=form,
                method="POST",
                headers={
                    "Authorization": f"Basic {credentials}",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                },
            )
            try:
                with urlopen(request, timeout=20) as response:
                    payload = json.load(response)
            except HTTPError as exc:
                raise DemoError(
                    "IdP 拒绝登录，请检查 DEMO_USERNAME、DEMO_PASSWORD 和客户端密钥"
                ) from exc
            except (URLError, TimeoutError, json.JSONDecodeError) as exc:
                raise DemoError("无法连接自建 IdP") from exc
            token = str(payload.get("access_token") or "")
            if not token:
                raise DemoError("IdP 响应中没有 access_token")
            expires_in = max(120, int(payload.get("expires_in") or 3600))
            self._token = token
            self._token_expires_at = now + expires_in
            return token

    def _runtime_client(self, token: str):
        try:
            client = self._session.client(
                "bedrock-agentcore",
                config=BotoConfig(
                    connect_timeout=20,
                    read_timeout=300,
                    retries={"max_attempts": 2, "mode": "standard"},
                ),
            )
        except (BotoCoreError, ClientError, NoCredentialsError) as exc:
            raise DemoError("AWS 凭证不可用，请重新执行 aws sso login") from exc

        def add_bearer(request, **_kwargs):
            request.headers["Authorization"] = f"Bearer {token}"

        client.meta.events.register_first(
            "before-send.bedrock-agentcore.*", add_bearer
        )
        return client

    def invoke(
        self,
        payload: dict[str, Any],
        *,
        session_id: str,
        qualifier: str,
    ) -> dict[str, Any]:
        token = self.get_token()
        client = self._runtime_client(token)
        try:
            return client.invoke_agent_runtime(
                agentRuntimeArn=self.config.runtime_arn,
                qualifier=qualifier,
                runtimeSessionId=session_id,
                payload=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                contentType="application/json",
            )
        except NoCredentialsError as exc:
            raise DemoError("AWS 凭证不可用，请重新执行 aws sso login") from exc
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in {"AccessDeniedException", "UnrecognizedClientException"}:
                raise DemoError("Runtime 拒绝访问，请检查 SSO 和 CUSTOM_JWT 配置") from exc
            raise DemoError(f"Runtime 调用失败（{code or 'ClientError'}）") from exc
        except BotoCoreError as exc:
            raise DemoError("连接 AgentCore Runtime 失败") from exc

    def list_resources(self, resource_type: str) -> dict[str, Any]:
        """只读列出合成业务资源；永不访问 AuthTable 或 MemoryTable。"""
        from boto3.dynamodb.types import TypeDeserializer
        from decimal import Decimal

        schemas = {
            "orders": {
                "prefix": "ORDER#",
                "label": "订单",
                "fields": (
                    "order_id", "customer_id", "status", "amount_cents", "currency",
                    "item_name", "qty", "created_at", "promised_at", "shipment_no",
                    "carrier",
                ),
                "sort": "order_id",
                "reverse": False,
            },
            "shipments": {
                "prefix": "SHIPMENT#",
                "label": "运单",
                "fields": (
                    "shipment_no", "order_id", "carrier", "status", "current_location",
                    "stalled_hours", "events",
                ),
                "sort": "shipment_no",
                "reverse": False,
            },
            "tickets": {
                "prefix": "TICKET#",
                "label": "工单",
                "fields": (
                    "ticket_id", "order_id", "category", "severity", "summary",
                    "status", "created_at",
                ),
                "sort": "created_at",
                "reverse": True,
            },
        }
        kind = str(resource_type or "").lower()
        schema = schemas.get(kind)
        if schema is None:
            raise DemoError("只允许查看 orders、shipments 或 tickets")

        try:
            client = self._session.client(
                "dynamodb",
                config=BotoConfig(connect_timeout=10, read_timeout=20),
            )
            items: list[dict[str, Any]] = []
            cursor = None
            while len(items) < 200:
                kwargs: dict[str, Any] = {
                    "TableName": f"{self.config.project}-business",
                    "FilterExpression": "begins_with(PK, :prefix) AND SK = :meta",
                    "ExpressionAttributeValues": {
                        ":prefix": {"S": schema["prefix"]},
                        ":meta": {"S": "META"},
                    },
                    "ConsistentRead": False,
                }
                if cursor:
                    kwargs["ExclusiveStartKey"] = cursor
                response = client.scan(**kwargs)
                items.extend(response.get("Items") or [])
                cursor = response.get("LastEvaluatedKey")
                if not cursor:
                    break
        except (BotoCoreError, ClientError, NoCredentialsError) as exc:
            raise DemoError("无法读取业务资源，请检查 AWS SSO 登录状态") from exc

        deserializer = TypeDeserializer()

        def json_safe(value: Any) -> Any:
            if isinstance(value, Decimal):
                return int(value) if value == value.to_integral_value() else float(value)
            if isinstance(value, list):
                return [json_safe(item) for item in value]
            if isinstance(value, dict):
                return {str(key): json_safe(item) for key, item in value.items()}
            return value

        public_items: list[dict[str, Any]] = []
        for raw in items[:200]:
            public: dict[str, Any] = {}
            for field in schema["fields"]:
                if field in raw:
                    public[field] = json_safe(deserializer.deserialize(raw[field]))
            # 运单轨迹只允许三个展示字段，避免未来新增内部字段时被自动公开。
            if kind == "shipments" and isinstance(public.get("events"), list):
                public["events"] = [
                    {
                        key: event.get(key)
                        for key in ("at", "location", "note")
                        if isinstance(event, dict) and key in event
                    }
                    for event in public["events"]
                    if isinstance(event, dict)
                ]
            public_items.append(public)
        public_items.sort(
            key=lambda item: item.get(schema["sort"], 0),
            reverse=bool(schema["reverse"]),
        )
        return {
            "resource": kind,
            "label": schema["label"],
            "count": len(public_items),
            "truncated": len(items) >= 200 and bool(cursor),
            "items": public_items,
            "refreshedAt": int(time.time()),
            "source": f"DynamoDB · {self.config.project}-business（只读）",
        }

    def run_selftest(self, *, qualifier: str) -> dict[str, Any]:
        response = self.invoke(
            {"mode": "selftest"},
            session_id=new_session_id(),
            qualifier=qualifier,
        )
        stream = response.get("response")
        if stream is None:
            raise DemoError("Runtime 自检没有返回响应体")
        raw = stream.read()
        if len(raw) > 5 * 1024 * 1024:
            raise DemoError("Runtime 自检响应过大")
        try:
            result = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise DemoError("Runtime 自检返回了无效 JSON") from exc
        if not isinstance(result, dict):
            raise DemoError("Runtime 自检响应格式不正确")
        return result


def _safe_log_error(exc: BaseException) -> None:
    print(f"[demo-web] {type(exc).__name__}: {exc}", file=sys.stderr)
    if os.environ.get("DEMO_WEB_DEBUG") == "1":
        traceback.print_exc()


class DemoServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, service: DemoService):
        super().__init__(address, handler)
        self.service = service
        self.static_html = STATIC_FILE.read_bytes()


class DemoHandler(BaseHTTPRequestHandler):
    server: DemoServer
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[demo-web] {self.address_string()} - {fmt % args}", file=sys.stderr)

    def _security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'",
        )

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _request_is_local(self) -> bool:
        host_header = self.headers.get("Host", "")
        host = host_header
        if host.startswith("[") and "]" in host:
            host = host[1 : host.index("]")]
        elif ":" in host:
            host = host.rsplit(":", 1)[0]
        if host.lower() not in LOOPBACK_HOSTS:
            return False
        origin = self.headers.get("Origin")
        if origin:
            parsed = urlparse(origin)
            if parsed.scheme != "http" or (parsed.hostname or "").lower() not in LOOPBACK_HOSTS:
                return False
        return True

    def _read_json(self) -> dict[str, Any]:
        if not self._request_is_local():
            raise DemoError("只接受来自本机页面的请求")
        if self.headers.get("X-Demo-Request") != "1":
            raise DemoError("缺少本地演示请求标记")
        content_type = self.headers.get("Content-Type", "")
        if not content_type.lower().startswith("application/json"):
            raise DemoError("Content-Type 必须是 application/json")
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise DemoError("Content-Length 不合法") from exc
        if size <= 0 or size > MAX_REQUEST_BYTES:
            raise DemoError("请求体为空或过大")
        try:
            payload = json.loads(self.rfile.read(size))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DemoError("请求体不是有效 JSON") from exc
        if not isinstance(payload, dict):
            raise DemoError("请求体必须是 JSON 对象")
        return payload

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/":
            body = self.server.static_html
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self._security_headers()
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/config":
            self._send_json(HTTPStatus.OK, self.server.service.config.public_dict())
            return
        if path == "/api/health":
            self._send_json(HTTPStatus.OK, {"status": "ok", "scope": "localhost"})
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        try:
            payload = self._read_json()
            if path == "/api/chat":
                self._handle_chat(payload)
                return
            if path == "/api/selftest":
                self._handle_selftest(payload)
                return
            if path == "/api/resources":
                result = self.server.service.list_resources(
                    str(payload.get("resource") or "").lower()
                )
                self._send_json(HTTPStatus.OK, result)
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
        except DemoError as exc:
            _safe_log_error(exc)
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:  # noqa: BLE001
            _safe_log_error(exc)
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "本地演示服务发生未预期错误，请查看启动终端"},
            )

    def _start_sse(self) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Connection", "close")
        self._security_headers()
        self.end_headers()
        self.close_connection = True

    def _write_sse(self, payload: dict[str, Any]) -> None:
        raw = "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"
        self.wfile.write(raw.encode("utf-8"))
        self.wfile.flush()

    def _handle_chat(self, request: dict[str, Any]) -> None:
        prompt = validate_prompt(request.get("prompt"))
        session_id = validate_session_id(request.get("sessionId"))
        qualifier = validate_qualifier(
            request.get("qualifier"), self.server.service.config.default_qualifier
        )
        # 先完成鉴权与 Runtime 握手，失败时仍可返回普通 JSON 状态码。
        response = self.server.service.invoke(
            {"mode": "stream", "prompt": prompt},
            session_id=session_id,
            qualifier=qualifier,
        )
        stream = response.get("response")
        if stream is None:
            raise DemoError("Runtime 没有返回响应体")

        self._start_sse()
        self._write_sse(
            {
                "type": "meta",
                "sessionId": session_id,
                "qualifier": qualifier,
                "runtimeId": self.server.service.config.runtime_id,
            }
        )
        content_type = str(response.get("contentType") or "")
        try:
            if "text/event-stream" in content_type:
                for chunk in stream.iter_chunks(chunk_size=64):
                    if chunk:
                        self.wfile.write(chunk)
                        self.wfile.flush()
            else:
                raw = stream.read()
                try:
                    result = json.loads(raw)
                except json.JSONDecodeError:
                    result = {"answer": raw.decode("utf-8", errors="replace")}
                text = result.get("answer") or result.get("error") or str(result)
                self._write_sse({"type": "text", "delta": text})
                self._write_sse({"type": "done", "sessionId": session_id})
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:  # noqa: BLE001
            _safe_log_error(exc)
            try:
                self._write_sse(
                    {"type": "error", "message": "流式连接中断，请查看启动终端"}
                )
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _handle_selftest(self, request: dict[str, Any]) -> None:
        qualifier = validate_qualifier(
            request.get("qualifier"), self.server.service.config.default_qualifier
        )
        result = self.server.service.run_selftest(qualifier=qualifier)
        self._send_json(HTTPStatus.OK, result)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765, help="本地端口，默认 8765")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port 必须在 1..65535")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    load_dotenv(REPO_ROOT / ".env")
    if not STATIC_FILE.exists():
        print(f"缺少页面文件：{STATIC_FILE}", file=sys.stderr)
        return 1
    try:
        config = AppConfig.from_env()
        service = DemoService(config)
        server = DemoServer(("127.0.0.1", args.port), DemoHandler, service)
    except (DemoError, OSError) as exc:
        print(f"启动失败：{exc}", file=sys.stderr)
        return 1

    url = f"http://127.0.0.1:{args.port}"
    print("\nAgentCore 客户演示台已启动")
    print(f"  地址：{url}")
    print(f"  Runtime：{config.runtime_id}")
    print(f"  Endpoint：{config.default_qualifier}")
    print("  安全边界：仅监听 127.0.0.1，凭证不会发送到浏览器")
    print("  停止：Ctrl+C\n")
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("\n正在停止演示台…")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
