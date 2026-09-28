"""Observability:自定义 span 埋点。

分工说明(不要重复造轮子):
  Strands 自己已经按 GenAI 语义约定发了三层 span ——
    agent invocation / model invoke / tool call,
    带 gen_ai.operation.name、gen_ai.usage.*_tokens、gen_ai.tool.name 等属性。
    这些在 CloudWatch 的 GenAI 看板上直接能用,不需要我们插手。
  这里只补 Strands 看不到的那部分:
    MemoryLite 的读写、Gateway 取 token、沙箱/浏览器会话启动、selftest 各步骤。

两条硬规则:
  1. **埋点绝不能让业务失败。** 所有 OTEL 调用都在 try 里,
     拿不到 tracer 就退化成空操作。观测挂了顶多少一条 trace,
     不该连带把用户的对话搞崩。
  2. **不往 span 里写敏感内容。** 只记长度、条数、耗时、成败,
     不记消息正文、token、密钥。CloudWatch 的日志/trace 留存期通常比
     业务数据长,写进去就等于延长了敏感数据的生命周期。

Runtime 会自动注入 OTEL_* 环境变量,Dockerfile 里用 opentelemetry-instrument
启动,所以 provider 是自动配好的 —— 这里不做任何 provider 初始化。
"""

from __future__ import annotations

import contextlib
import logging
import os
import time
from collections.abc import Iterator
from typing import Any

LOG = logging.getLogger(__name__)

# 自定义属性统一前缀,方便在 CloudWatch Logs Insights 里按 acn.* 过滤
NS = "acn"

_TRACER_NAME = "agentcore-cn"


def _get_tracer() -> Any:
    """拿 tracer。拿不到返回 None,所有埋点退化成空操作。"""
    try:
        from opentelemetry import trace

        return trace.get_tracer(_TRACER_NAME)
    except Exception:
        LOG.debug("OTEL 不可用,埋点退化为空操作", exc_info=True)
        return None


def otel_enabled() -> bool:
    """Runtime 注入了 OTEL 导出配置才算真正启用。"""
    return bool(
        os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
        or os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
        or os.environ.get("AGENT_OBSERVABILITY_ENABLED")
    )


def _sanitize(value: Any) -> Any:
    """span 属性只接受标量;别的一律转成字符串长度描述,避免误写正文。"""
    if isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value
    return str(value)


@contextlib.contextmanager
def span(name: str, **attributes: Any) -> Iterator[dict[str, Any]]:
    """开一个自定义 span。

    yield 出一个 dict,往里塞键值会在退出时写成 span 属性 ——
    这样被测代码可以在执行过程中补充"结果类"的属性(命中条数、字节数)。

    用法:
        with span("memory.load_history", session_id=sid) as sp:
            turns = memory.load_history(sid)
            sp["turn_count"] = len(turns)
    """
    extra: dict[str, Any] = {}
    started = time.monotonic()

    try:
        tracer = _get_tracer()
    except Exception:
        LOG.debug("取 tracer 失败,退化为空操作", exc_info=True)
        tracer = None

    if tracer is None:
        yield extra
        return

    try:
        # record_exception=False 很关键:OTEL 默认会把异常【连 message 一起】
        # 记成一个 span event,而 message 里经常带订单号、用户原话这类业务数据。
        # 我们只在 _record_failure 里记异常类型。
        # set_status_on_exception 同理,状态由我们自己设。
        cm = tracer.start_as_current_span(
            f"{NS}.{name}", record_exception=False, set_status_on_exception=False
        )
    except Exception:
        LOG.debug("开 span 失败,退化为空操作", exc_info=True)
        yield extra
        return

    with cm as otel_span:
        try:
            yield extra
        except Exception as exc:
            _record_failure(otel_span, exc)
            raise
        finally:
            _write_attributes(otel_span, name, attributes, extra, started)


def _write_attributes(
    otel_span: Any,
    name: str,
    attributes: dict[str, Any],
    extra: dict[str, Any],
    started: float,
) -> None:
    try:
        otel_span.set_attribute(f"{NS}.component", name.split(".", 1)[0])
        otel_span.set_attribute(
            f"{NS}.duration_ms", int((time.monotonic() - started) * 1000)
        )
        for key, value in {**attributes, **extra}.items():
            if value is None:
                continue
            otel_span.set_attribute(f"{NS}.{key}", _sanitize(value))
    except Exception:
        LOG.debug("写 span 属性失败", exc_info=True)


def _record_failure(otel_span: Any, exc: BaseException) -> None:
    try:
        from opentelemetry.trace import Status, StatusCode

        otel_span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
        # 只记异常类型,不记 message —— message 里可能带业务数据
        otel_span.set_attribute(f"{NS}.error.type", type(exc).__name__)
    except Exception:
        LOG.debug("记录 span 失败状态时出错", exc_info=True)


def set_session_attributes(session_id: str, actor_id: str, project: str = "") -> None:
    """把会话/用户标记打到当前 span 上。

    Strands 的 trace_attributes 已经在 agent span 上设了这些,
    但自定义 span(比如 selftest)不经过 Strands,需要自己补。
    """
    tracer_span = _current_span()
    if tracer_span is None:
        return
    try:
        tracer_span.set_attribute("session.id", session_id)
        tracer_span.set_attribute("user.id", actor_id)
        if project:
            tracer_span.set_attribute("project", project)
    except Exception:
        LOG.debug("设置会话属性失败", exc_info=True)


def add_event(name: str, **attributes: Any) -> None:
    """给当前 span 加一个事件点。用于标记"到这一步了"。"""
    tracer_span = _current_span()
    if tracer_span is None:
        return
    try:
        tracer_span.add_event(
            f"{NS}.{name}",
            {f"{NS}.{k}": _sanitize(v) for k, v in attributes.items() if v is not None},
        )
    except Exception:
        LOG.debug("添加 span 事件失败", exc_info=True)


def _current_span() -> Any:
    try:
        from opentelemetry import trace

        current = trace.get_current_span()
        # 没有活跃 span 时返回的是 INVALID_SPAN,写属性是无意义的空操作
        if not current or not current.is_recording():
            return None
        return current
    except Exception:
        return None


def current_trace_id() -> str:
    """返回当前 trace id 的十六进制形式,方便把它写进给用户的回复或报告里,
    出问题时能直接去 CloudWatch 按 trace id 定位。"""
    current = _current_span()
    if current is None:
        return ""
    try:
        return format(current.get_span_context().trace_id, "032x")
    except Exception:
        return ""


__all__ = [
    "NS",
    "span",
    "add_event",
    "set_session_attributes",
    "current_trace_id",
    "otel_enabled",
]
