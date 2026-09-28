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

import asyncio
import inspect
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


async def _run_step_async(report: Report, name: str, component: str, fn) -> StepResult:
    """跑一步。无论成败都记录并继续 —— 这是自检的核心语义。

    fn 可以是同步或 async:
      * async 的直接 await —— Browser 这类本身就是异步的,
        绝不能再包 asyncio.run,那会开出嵌套循环并挂死(见 _check_browser_async)
      * 同步的丢到线程里,别卡住事件循环
    """

    started = time.monotonic()
    with obs.span(f"selftest.{component}", step=name) as sp:
        try:
            # 这些检查项是 lambda,包着同步或 async 函数。
            # 只调用一次,再看返回值是不是可等待的 —— 用
            # iscoroutinefunction(fn) 判断不行(lambda 本身是同步的),
            # 调两次也不行(会重复执行探测)。
            # 同步的检查项(boto3 调用)放到线程里,别阻塞事件循环;
            # async 的(Browser)直接 await —— 绝不能再包 asyncio.run,
            # 那会开出嵌套循环并挂死,见 _check_browser_async 的说明。
            outcome = await asyncio.to_thread(fn)
            if inspect.isawaitable(outcome):
                # async 函数在线程里只是拿到了协程对象,回到本循环再 await
                outcome = await outcome
            detail = outcome or ""
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


def _run_step(report: Report, name: str, component: str, fn) -> StepResult:
    """同步包装,给测试和同步调用方用。

    真正的实现是 _run_step_async —— selftest 整体是 async 的,
    但直接暴露一个 async 函数会让每个测试都要包 asyncio.run,
    而这个函数本身不涉及嵌套事件循环的问题(它只是转发)。
    """
    return asyncio.run(_run_step_async(report, name, component, fn))


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


async def _check_browser_async(settings: Settings) -> str:
    """Browser 探测。

    【必须是 async 的】早先这里写成同步函数 + asyncio.run(probe()),
    结果整步挂死:_run_step 本身已经被 asyncio.to_thread 放进工作线程,
    里面再 asyncio.run 就又开了一个事件循环;ExitStack 退出时
    LazyBrowser.close() 检测到"不在循环里",于是【第三次】asyncio.run,
    去关一个绑在已关闭循环上的 Playwright 对象 —— 直接卡住,
    连异常日志都走不到,表现成整次自检静默超时。

    Browser 工具本来就是 async 的,顺着 await 下去就行,不要自己造循环。
    """
    if not settings.logistics_url:
        raise _Skip("LOGISTICS_URL 未配置")

    import contextlib as _ctx

    from agent.tools.browser import build_browser_tools

    with _ctx.ExitStack() as stack:
        tools = {t.tool_name: t for t in build_browser_tools(settings, stack)}
        result = str(await tools["track_shipment"](shipment_no="SF7758291046"))
    if "查询失败" in result or "无法查询" in result or "超时" in result:
        raise RuntimeError(result[:200])
    return f"抓到页面内容 {len(result)} 字符"


# ---------------------------------------------------------------------------
# 报告落盘
# ---------------------------------------------------------------------------


_S3_CLIENT: Any = None


def _get_s3_client(settings: Settings) -> Any:
    """拿一个配好超时的 S3 客户端,进程内复用。

    刻意不在 _publish_report 内部临时 boto3.client():那样每次都要
    加载 endpoint 数据、走一遍凭证链,这些开销在 read_timeout 的覆盖
    范围【之外】,卡住时超时配置管不着。
    """
    global _S3_CLIENT
    if _S3_CLIENT is None:
        import boto3
        from botocore.config import Config

        _S3_CLIENT = boto3.client(
            "s3",
            region_name=settings.region,
            config=Config(
                connect_timeout=3,
                read_timeout=5,
                # 报告上传是增强项,不值得重试
                retries={"max_attempts": 1, "mode": "standard"},
            ),
        )
    return _S3_CLIENT


def _publish_report(
    settings: Settings, report: Report, session_id: str, actor_id: str
) -> str:
    """把报告写 S3 并返回预签名链接。失败返回空串 —— 报告本身还在响应体里。

    【实测踩过的坑,排查了四轮】这一步曾把整次 selftest 拖到客户端读超时:
    8 个步骤 6.6 秒全跑完,然后这里静默挂住,一条日志都没有。

    确定性证据:日志里「自检报告已上传」和「自检完成」两行都不出现,
    而把 ARTIFACT_BUCKET 清空(让本函数直接 return)后,整次自检
    从 6 分钟超时变成 14 秒返回。

    叠加的原因:
      1. boto3 默认重试 5 次、指数退避、没有 read timeout
      2. 在函数内部临时 boto3.client() —— 首次创建要加载 endpoint 数据
         和解析凭证链,这部分不受 read_timeout 约束
      3. 整个调用在 async entrypoint 里同步阻塞
      4. except 只在重试全部用尽后才触发,期间毫无日志

    所以:客户端预建复用(见 _get_s3_client)、超时收到 3/5 秒、
    不重试、调用方用 asyncio.to_thread 移出事件循环。
    """
    if not settings.artifact_bucket:
        return ""
    key = posixpath.join(
        "outputs", actor_id or "anonymous", session_id or "no-session",
        f"selftest-{report.started_at}.md",
    )
    started = time.monotonic()
    try:
        s3 = _get_s3_client(settings)
        s3.put_object(
            Bucket=settings.artifact_bucket,
            Key=key,
            Body=report.to_markdown().encode("utf-8"),
            ContentType="text/markdown; charset=utf-8",
            ServerSideEncryption="AES256",
        )
        url = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": settings.artifact_bucket, "Key": key},
            ExpiresIn=3600,
        )
        LOG.info("自检报告已上传(%.1fs)", time.monotonic() - started)
        return url
    except Exception:
        LOG.exception("上传自检报告失败(%.1fs),报告仍在响应体里",
                      time.monotonic() - started)
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
        ("网页抓取", "Browser", lambda: _check_browser_async(settings)),
    ]


async def run_selftest(
    settings: Settings, *, session_id: str, actor_id: str
) -> dict[str, Any]:
    """依次跑完所有检查,返回结构化结果。

    整体包在一个 span 里,所以在 Observability 看板上这次自检是一条完整 trace。
    """
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
            # _run_step 自己判断 fn 是同步还是 async
            await _run_step_async(report, name, component, fn)

        sp["steps_total"] = len(report.steps)
        sp["steps_ok"] = report.ok_count
        sp["steps_failed"] = len(report.failed)

    payload = report.to_dict()
    # 报告内容【始终】在响应体里 —— S3 链接只是方便分享的增强项。
    # 这个顺序很关键:先把结果装好,上传失败也不影响调用方拿到完整报告。
    payload["markdown"] = report.to_markdown()

    # 上传放在最后,而且是阻塞的 boto3 调用 -> 必须移出事件循环。
    #
    # 不用 asyncio.wait_for 包:to_thread 里的阻塞调用不可取消,
    # wait_for 超时只会让调用方不再等待,线程仍跑到底并拖住进程退出
    # (实测测试因此白等 30 秒)。真正的硬约束是 _get_s3_client 里配的
    # connect=3 / read=5 / 不重试,最坏约 8 秒。
    try:
        url = await asyncio.to_thread(
            _publish_report, settings, report, session_id, actor_id
        )
        if url:
            payload["reportUrl"] = url
    except Exception:
        # 上传抛异常也不能让整次自检失败 —— 报告已经在 payload 里了
        LOG.exception("上传自检报告时出错,报告仍在响应体里")

    LOG.info("自检完成:%d/%d 通过", report.ok_count, len(report.steps))
    return payload


__all__ = ["run_selftest", "Report", "StepResult", "build_steps"]
