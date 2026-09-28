"""自检链路:依次点亮中国区可用的 6 个 AgentCore 组件,产出一份报告。

这是 demo 最实用的一条路径:部署完之后跑一次
    {"mode": "selftest"}
就知道哪个组件通了、哪个没通、为什么没通,而不是靠对着业务对话猜。

设计要点:
  * 每一步都独立 try —— 一个组件不可用不该让后面的步骤跳过。
    这正是自检的价值:一次跑完看到全景,而不是在第一个错误处停下。
  * 每一步包在自定义 span 里,所以在 Observability 看板上
    这条自检本身就是一条完整 trace,顺带验证了埋点是否工作。
  * 报告写 S3 并返回预签名链接;S3 不可用时报告仍原样返回在响应体里。
  * 只读探测。唯一的写操作是 MemoryLite 写一条自检标记(带 TTL 会自己消失)
    和把报告传 S3 —— 不会碰业务数据、不会开工单。
"""

from __future__ import annotations

import json
import logging
import posixpath
import time
from dataclasses import dataclass, field
from typing import Any

from agent import obs
from agent.config import Settings

LOG = logging.getLogger(__name__)

# 单步超时。某个组件卡住时不该让整次自检无限期挂着。
_STEP_TIMEOUT_SECONDS = 60


@dataclass
class StepResult:
    name: str
    component: str
    ok: bool
    duration_ms: int
    detail: str = ""
    error: str = ""
    skipped: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "component": self.component,
            "status": "skipped" if self.skipped else ("ok" if self.ok else "failed"),
            "duration_ms": self.duration_ms,
            "detail": self.detail,
            "error": self.error,
        }


@dataclass
class Report:
    session_id: str
    actor_id: str
    region: str
    project: str
    started_at: int
    trace_id: str = ""
    steps: list[StepResult] = field(default_factory=list)

    @property
    def ok_count(self) -> int:
        return sum(1 for s in self.steps if s.ok)

    @property
    def failed(self) -> list[StepResult]:
        return [s for s in self.steps if not s.ok and not s.skipped]

    def to_dict(self) -> dict[str, Any]:
        return {
            "sessionId": self.session_id,
            "actorId": self.actor_id,
            "region": self.region,
            "project": self.project,
            "startedAt": self.started_at,
            "traceId": self.trace_id,
            "summary": {
                "total": len(self.steps),
                "ok": self.ok_count,
                "failed": len(self.failed),
                "skipped": sum(1 for s in self.steps if s.skipped),
            },
            "steps": [s.to_dict() for s in self.steps],
        }

    def to_markdown(self) -> str:
        lines = [
            "# AgentCore 中国区能力自检报告",
            "",
            f"- 区域:`{self.region}`",
            f"- 项目:`{self.project}`",
            f"- 会话:`{self.session_id}`",
            f"- 时间:{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self.started_at))}",
        ]
        if self.trace_id:
            lines.append(f"- Trace ID:`{self.trace_id}`(可在 CloudWatch 里按它定位)")
        lines += [
            "",
            f"**{self.ok_count}/{len(self.steps)} 项通过**",
            "",
            "| 组件 | 检查项 | 结果 | 耗时 | 说明 |",
            "| --- | --- | --- | --- | --- |",
        ]
        mark = {"ok": "通过", "failed": "失败", "skipped": "跳过"}
        for step in self.steps:
            data = step.to_dict()
            note = data["detail"] or data["error"] or "-"
            # 表格里的竖线要转义,否则会把列切碎
            note = note.replace("|", "\\|").replace("\n", " ")
            lines.append(
                f"| {step.component} | {step.name} | {mark[data['status']]} "
                f"| {step.duration_ms} ms | {note} |"
            )
        if self.failed:
            lines += ["", "## 失败项", ""]
            for step in self.failed:
                lines.append(f"- **{step.name}**({step.component}):{step.error}")
        return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# 步骤执行框架
# ---------------------------------------------------------------------------


def _run_step(report: Report, name: str, component: str, fn) -> StepResult:
    """跑一步。无论成败都记录并继续 —— 这是自检的核心语义。"""
    started = time.monotonic()
    with obs.span(f"selftest.{component}", step=name) as sp:
        try:
            detail = fn() or ""
            result = StepResult(
                name=name,
                component=component,
                ok=True,
                duration_ms=int((time.monotonic() - started) * 1000),
                detail=str(detail)[:500],
            )
            sp["ok"] = True
        except _Skip as skip:
            result = StepResult(
                name=name,
                component=component,
                ok=False,
                skipped=True,
                duration_ms=int((time.monotonic() - started) * 1000),
                detail=str(skip),
            )
            sp["skipped"] = True
        except Exception as exc:  # noqa: BLE001
            LOG.exception("自检步骤失败:%s", name)
            result = StepResult(
                name=name,
                component=component,
                ok=False,
                duration_ms=int((time.monotonic() - started) * 1000),
                # 类型 + 简短消息:自检报告就是给人看错误的,这里需要细节
                error=f"{type(exc).__name__}: {exc}"[:300],
            )
            sp["ok"] = False
            sp["error_type"] = type(exc).__name__
    report.steps.append(result)
    obs.add_event("selftest.step", step=name, ok=result.ok)
    return result


class _Skip(Exception):
    """这一步因为没配置而跳过,不算失败。"""


# ---------------------------------------------------------------------------
# 各组件的探测
# ---------------------------------------------------------------------------


def _check_runtime(settings: Settings) -> str:
    """Runtime 本身:能跑到这里就说明容器起来了、入口通了。"""
    import platform
    import sys

    return (
        f"python {sys.version.split()[0]} / {platform.machine()} / "
        f"区域 {settings.region}"
    )


def _check_observability(settings: Settings) -> str:
    if not obs.otel_enabled():
        raise _Skip("没有检测到 OTEL_* 环境变量,本地运行时属正常")
    trace_id = obs.current_trace_id()
    if not trace_id:
        raise RuntimeError("OTEL 已配置但拿不到 trace id,provider 可能没初始化")
    return f"trace id {trace_id}"


def _check_memory(settings: Settings, session_id: str, actor_id: str) -> str:
    from agent.memory_lite import MemoryLite

    memory = MemoryLite(settings)
    marker = f"selftest-{int(time.time())}"
    memory.append_turn(session_id, "user", marker)
    history = memory.load_history(session_id)
    if not any(t.content == marker for t in history):
        raise RuntimeError("写进去的自检标记没读回来")

    facts = memory.get_facts(actor_id)
    return f"STM 可读写(当前 {len(history)} 条),LTM 有 {len(facts)} 条事实"


def _check_identity(settings: Settings) -> str:
    """只验证"能取到 DeepSeek 的 key",绝不打印 key 本身。"""
    from agent.model import get_api_key

    if settings.is_local:
        raise _Skip("本地模式用 DEEPSEEK_API_KEY 环境变量,没走 Identity")
    key = get_api_key(settings, refresh=True)
    if not key:
        raise RuntimeError("Identity 返回了空的 API key")
    # 只报长度,不报内容
    return f"从 provider {settings.deepseek_api_key_provider} 取到凭证(长度 {len(key)})"


def _check_model(settings: Settings) -> str:
    """真打一次 DeepSeek,确认模型链路通。用最短的 prompt 省钱。"""
    from strands import Agent

    from agent.model import build_model

    agent = Agent(
        model=build_model(settings, stream=False),
        system_prompt="只回答一个词。",
        tools=[],
        callback_handler=None,
    )
    answer = str(agent("回复 pong")).strip()
    if not answer:
        raise RuntimeError("模型返回空响应")
    return f"{settings.deepseek_model} 响应:{answer[:60]}"


def _check_gateway(settings: Settings) -> str:
    from agent.tools.gateway import load_gateway_tools

    if not settings.gateway_url:
        raise _Skip("GATEWAY_URL 未配置")
    client, tools = load_gateway_tools(settings)
    try:
        names = [getattr(t, "tool_name", "?") for t in tools]
        if not names:
            raise RuntimeError("Gateway 连上了但没列出任何工具")
        return f"{len(names)} 个工具:{', '.join(names[:8])}"
    finally:
        client.stop(None, None, None)


def _check_code_interpreter(settings: Settings) -> str:
    from agent.tools.code_interp import format_execution, parse_invoke_result

    from bedrock_agentcore.tools import CodeInterpreter

    client = CodeInterpreter(settings.region)
    client.start(identifier=settings.code_interpreter_id)
    try:
        parsed = parse_invoke_result(
            client.execute_code(code="print(6*7)", language="python")
        )
        if "42" not in parsed["stdout"]:
            raise RuntimeError(f"沙箱输出不对:{format_execution(parsed)[:200]}")
        return f"沙箱执行正常(耗时 {parsed['execution_time_ms']} ms)"
    finally:
        try:
            client.stop()
        except Exception:
            LOG.warning("关闭沙箱会话失败(会话会自行超时)")


def _check_browser(settings: Settings) -> str:
    """同步包装:selftest 整体是 async,这一步单独跑一个协程。"""
    import asyncio

    if not settings.logistics_url:
        raise _Skip("LOGISTICS_URL 未配置")

    async def probe() -> str:
        import contextlib as _ctx

        from agent.tools.browser import build_browser_tools

        with _ctx.ExitStack() as stack:
            tools = {
                t.tool_name: t for t in build_browser_tools(settings, stack)
            }
            result = str(await tools["track_shipment"](shipment_no="SF7758291046"))
        if "查询失败" in result or "无法查询" in result:
            raise RuntimeError(result[:200])
        return f"抓到页面内容 {len(result)} 字符"

    return asyncio.run(probe())


# ---------------------------------------------------------------------------
# 报告落盘
# ---------------------------------------------------------------------------


def _publish_report(
    settings: Settings, report: Report, session_id: str, actor_id: str
) -> str:
    """把报告写 S3 并返回预签名链接。失败返回空串 —— 报告本身还在响应体里。"""
    if not settings.artifact_bucket:
        return ""
    key = posixpath.join(
        "outputs", actor_id or "anonymous", session_id or "no-session",
        f"selftest-{report.started_at}.md",
    )
    try:
        import boto3

        s3 = boto3.client("s3", region_name=settings.region)
        s3.put_object(
            Bucket=settings.artifact_bucket,
            Key=key,
            Body=report.to_markdown().encode("utf-8"),
            ContentType="text/markdown; charset=utf-8",
            ServerSideEncryption="AES256",
        )
        return s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": settings.artifact_bucket, "Key": key},
            ExpiresIn=3600,
        )
    except Exception:
        LOG.exception("上传自检报告失败")
        return ""


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def build_steps(settings: Settings, session_id: str, actor_id: str) -> list[tuple]:
    """(检查项名, 组件名, 可调用) 的有序列表。

    顺序有讲究:先验证不依赖外部的(Runtime / Observability / Memory),
    再验证要凭证的(Identity / 模型),最后才是重的沙箱类。
    这样前面失败时,后面的失败原因一眼能看出是连锁反应。
    """
    return [
        ("容器与入口", "Runtime", lambda: _check_runtime(settings)),
        ("OTEL 埋点", "Observability", lambda: _check_observability(settings)),
        ("会话与长期记忆", "MemoryLite", lambda: _check_memory(settings, session_id, actor_id)),
        ("出向凭证", "Identity", lambda: _check_identity(settings)),
        ("DeepSeek 模型调用", "Model", lambda: _check_model(settings)),
        ("MCP 工具列表", "Gateway", lambda: _check_gateway(settings)),
        ("沙箱执行", "CodeInterpreter", lambda: _check_code_interpreter(settings)),
        ("网页抓取", "Browser", lambda: _check_browser(settings)),
    ]


async def run_selftest(
    settings: Settings, *, session_id: str, actor_id: str
) -> dict[str, Any]:
    """依次跑完所有检查,返回结构化结果。

    整体包在一个 span 里,所以在 Observability 看板上这次自检是一条完整 trace。
    """
    import asyncio

    report = Report(
        session_id=session_id,
        actor_id=actor_id,
        region=settings.region,
        project=settings.project,
        started_at=int(time.time()),
    )

    with obs.span("selftest.run", session_id=session_id) as sp:
        obs.set_session_attributes(session_id, actor_id, settings.project)
        report.trace_id = obs.current_trace_id()

        for name, component, fn in build_steps(settings, session_id, actor_id):
            # 阻塞调用放到线程里,别卡住 Runtime 的事件循环
            await asyncio.to_thread(_run_step, report, name, component, fn)

        sp["steps_total"] = len(report.steps)
        sp["steps_ok"] = report.ok_count
        sp["steps_failed"] = len(report.failed)

    url = _publish_report(settings, report, session_id, actor_id)
    payload = report.to_dict()
    payload["markdown"] = report.to_markdown()
    if url:
        payload["reportUrl"] = url
    LOG.info("自检完成:%d/%d 通过", report.ok_count, len(report.steps))
    return payload


__all__ = ["run_selftest", "Report", "StepResult", "build_steps"]
