"""Observability 埋点的测试。

不 mock OTEL —— 用真实的 SDK 配一个 InMemorySpanExporter,
断言 span 真的产生了、属性真的写上了。mock 掉的话只能验证"调了某个函数",
验不出"span 名字对不对、属性有没有真写进去"。

两条硬规则各有对应测试:
  1. 埋点绝不能让业务失败 —— 没有 provider、OTEL 抛异常时都必须静默降级
  2. 不往 span 里写敏感内容 —— 有专门的断言扫描属性值
"""

from __future__ import annotations

import pytest


@pytest.fixture
def otel():
    """真实 OTEL SDK + 内存 exporter。返回 (取 span 的函数, 清空的函数)。"""
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    # 全局 provider 只能设一次,用内部变量强制替换
    trace._TRACER_PROVIDER = provider  # type: ignore[attr-defined]
    trace._TRACER_PROVIDER_SET_ONCE._done = False  # type: ignore[attr-defined]
    trace.set_tracer_provider(provider)

    yield exporter

    exporter.clear()
    trace._TRACER_PROVIDER = None  # type: ignore[attr-defined]
    trace._TRACER_PROVIDER_SET_ONCE._done = False  # type: ignore[attr-defined]


def spans_by_name(exporter) -> dict[str, object]:
    return {s.name: s for s in exporter.get_finished_spans()}


def attrs_of(span) -> dict:
    return dict(span.attributes or {})


# ---------------------------------------------------------------------------
# 基本行为
# ---------------------------------------------------------------------------


class TestSpanBasics:
    def test_span_is_emitted_with_namespaced_name(self, otel):
        from agent import obs

        with obs.span("memory.load_history"):
            pass

        assert "acn.memory.load_history" in spans_by_name(otel)

    def test_attributes_are_namespaced(self, otel):
        from agent import obs

        with obs.span("memory.load_history", max_turns=12):
            pass

        attrs = attrs_of(otel.get_finished_spans()[0])
        assert attrs["acn.max_turns"] == 12
        assert attrs["acn.component"] == "memory"
        assert "acn.duration_ms" in attrs

    def test_result_attributes_can_be_added_during_execution(self, otel):
        """yield 出来的 dict 让被测代码能补充"结果类"属性。"""
        from agent import obs

        with obs.span("memory.load_history") as sp:
            sp["turn_count"] = 7

        assert attrs_of(otel.get_finished_spans()[0])["acn.turn_count"] == 7

    def test_none_attributes_are_dropped(self, otel):
        from agent import obs

        with obs.span("x.y", present=1, absent=None):
            pass

        attrs = attrs_of(otel.get_finished_spans()[0])
        assert "acn.present" in attrs
        assert "acn.absent" not in attrs

    def test_nested_spans_form_a_tree(self, otel):
        from agent import obs

        with obs.span("selftest.run"):
            with obs.span("selftest.memory"):
                pass

        finished = otel.get_finished_spans()
        inner = next(s for s in finished if s.name == "acn.selftest.memory")
        outer = next(s for s in finished if s.name == "acn.selftest.run")
        assert inner.parent is not None
        assert inner.parent.span_id == outer.context.span_id

    def test_duration_is_recorded(self, otel):
        import time

        from agent import obs

        with obs.span("slow.thing"):
            time.sleep(0.02)

        assert attrs_of(otel.get_finished_spans()[0])["acn.duration_ms"] >= 15


# ---------------------------------------------------------------------------
# 失败处理
# ---------------------------------------------------------------------------


class TestErrorHandling:
    def test_exception_propagates(self, otel):
        """埋点不能吞掉业务异常。"""
        from agent import obs

        with pytest.raises(ValueError, match="业务失败"):
            with obs.span("x.y"):
                raise ValueError("业务失败")

    def test_span_is_marked_as_error(self, otel):
        from opentelemetry.trace import StatusCode

        from agent import obs

        with pytest.raises(ValueError):
            with obs.span("x.y"):
                raise ValueError("炸了")

        span = otel.get_finished_spans()[0]
        assert span.status.status_code == StatusCode.ERROR
        assert attrs_of(span)["acn.error.type"] == "ValueError"

    def test_error_message_is_not_recorded(self, otel):
        """只记异常类型 —— message 里可能带业务数据(订单号、用户输入)。"""
        from agent import obs

        canary = "ORD-1024-CANARY-secret"
        with pytest.raises(ValueError):
            with obs.span("x.y"):
                raise ValueError(canary)

        span = otel.get_finished_spans()[0]
        serialized = str(attrs_of(span)) + str(span.status.description)
        assert canary not in serialized

    def test_attributes_are_still_written_on_failure(self, otel):
        from agent import obs

        with pytest.raises(RuntimeError):
            with obs.span("x.y", attempt=3):
                raise RuntimeError("x")

        assert attrs_of(otel.get_finished_spans()[0])["acn.attempt"] == 3


class TestGracefulDegradation:
    def test_works_without_any_provider(self):
        """本地跑、没配 OTEL 时必须完全静默,不能抛。"""
        from agent import obs

        with obs.span("x.y", a=1) as sp:
            sp["b"] = 2
        obs.add_event("e", k=1)
        obs.set_session_attributes("s", "a", "p")
        assert obs.current_trace_id() == ""

    def test_tracer_failure_does_not_break_the_body(self, monkeypatch, otel):
        """tracer 本身抛异常时,被包的代码仍要执行完。"""
        from agent import obs

        monkeypatch.setattr(
            obs, "_get_tracer", lambda: (_ for _ in ()).throw(RuntimeError("坏了"))
        )
        ran = []
        with obs.span("x.y"):
            ran.append(True)
        assert ran == [True]

    def test_start_span_failure_does_not_break_the_body(self, monkeypatch, otel):
        from agent import obs

        class BrokenTracer:
            def start_as_current_span(self, name):
                raise RuntimeError("provider 挂了")

        monkeypatch.setattr(obs, "_get_tracer", lambda: BrokenTracer())
        ran = []
        with obs.span("x.y") as sp:
            sp["k"] = 1
            ran.append(True)
        assert ran == [True]


# ---------------------------------------------------------------------------
# 会话属性与 trace id
# ---------------------------------------------------------------------------


class TestSessionAttributes:
    def test_session_and_user_are_set_on_current_span(self, otel):
        """CloudWatch GenAI 看板靠 session.id / user.id 筛,
        所以属性名不能加 acn 前缀 —— 那是约定好的标准键。"""
        from agent import obs

        with obs.span("selftest.run"):
            obs.set_session_attributes("sess-1", "actor-1", "agentcore-cn")

        attrs = attrs_of(otel.get_finished_spans()[0])
        assert attrs["session.id"] == "sess-1"
        assert attrs["user.id"] == "actor-1"
        assert attrs["project"] == "agentcore-cn"

    def test_trace_id_is_available_inside_a_span(self, otel):
        from agent import obs

        with obs.span("x.y"):
            trace_id = obs.current_trace_id()

        assert len(trace_id) == 32
        assert int(trace_id, 16) != 0

    def test_trace_id_matches_the_exported_span(self, otel):
        from agent import obs

        with obs.span("x.y"):
            captured = obs.current_trace_id()

        exported = format(otel.get_finished_spans()[0].context.trace_id, "032x")
        assert captured == exported

    def test_events_are_recorded(self, otel):
        from agent import obs

        with obs.span("selftest.run"):
            obs.add_event("selftest.step", step="memory", ok=True)

        events = otel.get_finished_spans()[0].events
        assert events[0].name == "acn.selftest.step"
        assert dict(events[0].attributes)["acn.step"] == "memory"


class TestOtelEnabled:
    def test_false_without_env(self, monkeypatch):
        from agent import obs

        for key in (
            "OTEL_EXPORTER_OTLP_ENDPOINT",
            "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
            "AGENT_OBSERVABILITY_ENABLED",
        ):
            monkeypatch.delenv(key, raising=False)
        assert obs.otel_enabled() is False

    @pytest.mark.parametrize(
        "key",
        [
            "OTEL_EXPORTER_OTLP_ENDPOINT",
            "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
            "AGENT_OBSERVABILITY_ENABLED",
        ],
    )
    def test_true_with_any_of_the_env_vars(self, monkeypatch, key):
        """Runtime 注入哪个都算启用。"""
        from agent import obs

        monkeypatch.setenv(key, "1")
        assert obs.otel_enabled() is True


# ---------------------------------------------------------------------------
# 真实调用路径上的埋点
# ---------------------------------------------------------------------------


class TestInstrumentedPaths:
    def test_memory_operations_emit_spans(self, otel, fake_ddb_factory):
        from agent.config import get_settings
        from agent.memory_lite import MemoryLite

        memory = MemoryLite(get_settings(), client=fake_ddb_factory())
        memory.append_turn("s1", "user", "问题")
        memory.load_history("s1")

        names = spans_by_name(otel)
        assert "acn.memory.append_turn" in names
        assert "acn.memory.load_history" in names
        assert attrs_of(names["acn.memory.load_history"])["acn.turn_count"] == 1

    def test_message_content_is_never_put_on_a_span(self, otel, fake_ddb_factory):
        """只记长度,不记正文。trace 的留存期通常比业务数据长,
        写进去就等于延长了敏感数据的生命周期。"""
        from agent.config import get_settings
        from agent.memory_lite import MemoryLite

        canary = "用户的身份证号 CANARY-123456"
        memory = MemoryLite(get_settings(), client=fake_ddb_factory())
        memory.append_turn("s1", "user", canary)

        for span in otel.get_finished_spans():
            assert canary not in str(attrs_of(span))

        append = spans_by_name(otel)["acn.memory.append_turn"]
        assert attrs_of(append)["acn.content_len"] == len(canary)

    def test_sandbox_start_is_instrumented(self, otel, monkeypatch):
        import agent.tools.code_interp as ci
        import bedrock_agentcore.tools as tools_pkg
        from agent.config import get_settings

        class FakeCI:
            def __init__(self, region):
                pass

            def start(self, identifier=None):
                return "s"

            def stop(self):
                return True

        monkeypatch.setattr(tools_pkg, "CodeInterpreter", FakeCI)
        ci.LazySandbox(get_settings()).client()

        names = spans_by_name(otel)
        assert "acn.sandbox.start" in names
        assert attrs_of(names["acn.sandbox.start"])["acn.identifier"] == (
            "aws.codeinterpreter.v1"
        )
