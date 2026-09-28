#!/usr/bin/env python3
"""调用 AgentCore Runtime 的客户端。

覆盖 demo 的四种调用方式:
    --prompt "..."            同步
    --stream --prompt "..."   流式(SSE 逐帧打印)
    --async --prompt "..."    异步长任务,返回 taskId
    --selftest                全能力自检

两个容易踩的点:

1. **SigV4 入向必须带 runtimeUserId。**
   容器里取 Identity 出向凭证要用 workload access token,而这个 token 是
   AgentCore 根据调用者身份生成的。用 CUSTOM_JWT 入向时它从 JWT 推导;
   用 SigV4 时必须显式传 runtimeUserId,否则 SDK 会报
   "Workload access token has not been set"。

2. **会话 ID 要自己保持。** 同一个 runtimeSessionId 才能续上对话,
   换一个就是新会话(这正是 demo 演示"会话隔离"的方式)。
   本脚本把它写进 .agentcore-session 以便多次调用续聊。

用法:
    python scripts/invoke.py --arn <runtime-arn> --prompt "ORD-1024 到哪了"
    python scripts/invoke.py --arn ... --stream --prompt "帮我算赔付"
    python scripts/invoke.py --arn ... --selftest
    python scripts/invoke.py --arn ... --new-session --prompt "..."
    python scripts/invoke.py --arn ... --jwt-token "$TOKEN" --prompt "..."
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

REPO_ROOT = Path(__file__).resolve().parents[1]
SESSION_FILE = REPO_ROOT / ".agentcore-session"

# AgentCore 要求 runtimeSessionId 至少 33 个字符
_MIN_SESSION_ID_LEN = 33


def log(msg: str) -> None:
    print(f"\033[1;34m==>\033[0m {msg}", file=sys.stderr)


def warn(msg: str) -> None:
    print(f"\033[1;33m[!]\033[0m {msg}", file=sys.stderr)


def die(msg: str) -> None:
    print(f"\033[1;31m[x]\033[0m {msg}", file=sys.stderr)
    raise SystemExit(1)


def resolve_session_id(new_session: bool) -> str:
    """复用上次的会话 ID,这样多次调用能续上对话。"""
    if not new_session and SESSION_FILE.exists():
        existing = SESSION_FILE.read_text().strip()
        if len(existing) >= _MIN_SESSION_ID_LEN:
            return existing
    session_id = f"demo-{uuid.uuid4().hex}"  # 37 字符,满足下限
    SESSION_FILE.write_text(session_id)
    return session_id


def read_body(response: dict[str, Any]) -> tuple[str, bool]:
    """返回 (正文, 是否是 SSE)。

    response 字段是个流对象,读一次就没了,所以这里一次读完。
    """
    content_type = response.get("contentType", "") or ""
    stream = response.get("response")
    if stream is None:
        return "", False
    raw = stream.read() if hasattr(stream, "read") else bytes(stream)
    return raw.decode("utf-8", errors="replace"), "text/event-stream" in content_type


def print_sse(body: str) -> None:
    """把 SSE 帧解出来按类型打印。"""
    pieces: list[str] = []
    for line in body.splitlines():
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload:
            continue
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            print(payload, end="", flush=True)
            continue
        kind = event.get("type")
        if kind == "text":
            chunk = event.get("delta", "")
            pieces.append(chunk)
            print(chunk, end="", flush=True)
        elif kind == "tool":
            print(f"\n\033[2m[调用工具 {event.get('name')}]\033[0m", flush=True)
        elif kind == "error":
            print(f"\n\033[1;31m[错误] {event.get('message')}\033[0m", flush=True)
        elif kind == "done":
            print(flush=True)
    if not pieces:
        warn("没有收到任何文本帧,原始响应:")
        print(body[:2000], file=sys.stderr)


def print_selftest(payload: dict[str, Any]) -> None:
    summary = payload.get("summary", {})
    mark = {"ok": "\033[32m通过\033[0m", "failed": "\033[31m失败\033[0m",
            "skipped": "\033[33m跳过\033[0m"}
    print(f"\n{summary.get('ok', 0)}/{summary.get('total', 0)} 项通过 "
          f"(失败 {summary.get('failed', 0)},跳过 {summary.get('skipped', 0)})\n")
    for step in payload.get("steps", []):
        note = step.get("detail") or step.get("error") or ""
        print(f"  {step['component']:<17} {mark.get(step['status'], step['status'])}  "
              f"{step['duration_ms']:>6} ms  {note[:70]}")
    if payload.get("traceId"):
        print(f"\nTrace ID: {payload['traceId']}")
    if payload.get("reportUrl"):
        print(f"报告(1 小时内有效):{payload['reportUrl']}")


def invoke(
    client,
    *,
    arn: str,
    payload: dict[str, Any],
    session_id: str,
    user_id: str,
    qualifier: str,
    jwt_token: str,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "agentRuntimeArn": arn,
        "payload": json.dumps(payload).encode("utf-8"),
        "contentType": "application/json",
        "runtimeSessionId": session_id,
        "qualifier": qualifier,
    }
    if not jwt_token:
        # SigV4 入向:必须带 runtimeUserId,否则容器里拿不到
        # workload access token,Identity 出向会失败。
        # CUSTOM_JWT 入向不需要 —— token 由 attach_bearer_token 挂在请求头上,
        # AgentCore 从 JWT 里推导出调用者身份。
        kwargs["runtimeUserId"] = user_id
    return client.invoke_agent_runtime(**kwargs)


def attach_bearer_token(client, token: str) -> None:
    """给 botocore 请求加 Authorization 头。

    CUSTOM_JWT 入向时服务端要的是 Bearer token 而不是 SigV4 签名,
    boto3 没有对应参数,只能挂事件钩子。
    """

    def add_header(request, **_kwargs):
        request.headers["Authorization"] = f"Bearer {token}"

    client.meta.events.register_first("before-send.bedrock-agentcore.*", add_header)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arn", default=os.environ.get("AGENT_RUNTIME_ARN"),
                        help="Runtime ARN,也可用环境变量 AGENT_RUNTIME_ARN")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "cn-northwest-1"))
    parser.add_argument("--profile", default=os.environ.get("AWS_PROFILE") or None)
    parser.add_argument("--prompt", help="要问的问题")
    parser.add_argument("--stream", action="store_true", help="流式返回")
    parser.add_argument("--async", dest="async_mode", action="store_true",
                        help="异步长任务,立刻返回 taskId")
    parser.add_argument("--status", metavar="TASK_ID", help="查异步任务状态")
    parser.add_argument("--selftest", action="store_true", help="跑全能力自检")
    parser.add_argument("--new-session", action="store_true", help="开一个新会话")
    parser.add_argument("--qualifier", default="DEFAULT",
                        help="端点名。DEFAULT 跟最新版本,stable 是钉住的那个")
    parser.add_argument("--user-id", default=os.environ.get("DEMO_USERNAME", "demo-user"),
                        help="SigV4 入向时的 runtimeUserId")
    parser.add_argument("--jwt-token", default=os.environ.get("AGENT_JWT_TOKEN", ""),
                        help="CUSTOM_JWT 入向的 access token")
    parser.add_argument("--timeout", type=int, default=300,
                        help="读超时秒数。自检要启动沙箱和浏览器,默认 300")
    parser.add_argument("--wait", action="store_true",
                        help="配合 --async,轮询到任务结束")
    args = parser.parse_args()

    if not args.arn:
        die("缺少 --arn(或设置 AGENT_RUNTIME_ARN)")

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    # botocore 默认 read timeout 是 60 秒,而一次完整自检要启动沙箱和浏览器,
    # 实测会超。Runtime 侧允许长任务,客户端不该在这里先断。
    client = session.client(
        "bedrock-agentcore",
        config=BotoConfig(read_timeout=args.timeout, connect_timeout=20,
                          retries={"max_attempts": 2, "mode": "standard"}),
    )
    if args.jwt_token:
        attach_bearer_token(client, args.jwt_token)

    # ---- 组装 payload ----
    if args.selftest:
        payload: dict[str, Any] = {"mode": "selftest"}
    elif args.status:
        payload = {"mode": "status", "taskId": args.status}
    elif args.prompt:
        mode = "stream" if args.stream else ("async" if args.async_mode else "sync")
        payload = {"mode": mode, "prompt": args.prompt}
    else:
        die("要么给 --prompt,要么用 --selftest / --status")

    session_id = resolve_session_id(args.new_session)
    log(f"会话 {session_id}  端点 {args.qualifier}  "
        f"鉴权 {'JWT' if args.jwt_token else 'SigV4'}")

    try:
        response = invoke(
            client, arn=args.arn, payload=payload, session_id=session_id,
            user_id=args.user_id, qualifier=args.qualifier, jwt_token=args.jwt_token,
        )
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        message = exc.response.get("Error", {}).get("Message", str(exc))
        if code == "AccessDeniedException" and not args.jwt_token:
            warn("如果 Runtime 是 CUSTOM_JWT 入向,要用 --jwt-token 传 access token。"
                 "先从自建 IdP 的 /oauth2/token 拿一个。")
        die(f"{code}: {message}")

    body, is_sse = read_body(response)
    status = response.get("statusCode")
    if status and int(status) >= 400:
        die(f"Runtime 返回 {status}:{body[:500]}")

    # ---- 输出 ----
    if is_sse:
        print_sse(body)
        return 0

    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        print(body)
        return 0

    if args.selftest:
        print_selftest(parsed)
        return 0

    if "answer" in parsed:
        print(parsed["answer"])
        if parsed.get("traceId"):
            log(f"Trace ID {parsed['traceId']}")
        return 0

    # 异步模式:可选地轮询到结束
    if args.wait and parsed.get("taskId"):
        task_id = parsed["taskId"]
        log(f"任务 {task_id} 已提交,轮询中…")
        for _ in range(120):
            time.sleep(3)
            poll = invoke(
                client, arn=args.arn,
                payload={"mode": "status", "taskId": task_id},
                session_id=session_id, user_id=args.user_id,
                qualifier=args.qualifier, jwt_token=args.jwt_token,
            )
            state, _ = read_body(poll)
            data = json.loads(state)
            if data.get("status") != "running":
                print(json.dumps(data, indent=2, ensure_ascii=False))
                return 0
        die("轮询超时")

    print(json.dumps(parsed, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
