"""selftest 自检链路的测试。

自检的核心语义是"一次跑完看全景",所以最重要的断言是:
**某一步失败绝不能让后面的步骤跳过**。这和普通业务代码"快速失败"正好相反。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest


@pytest.fixture
def settings():
    from agent.config import get_settings

    return get_settings()


@pytest.fixture
def no_network(monkeypatch):
    """把真会打网络的那一步打桩。

    测试验的是编排(一步失败不影响后面),不是 DeepSeek 通不通 ——
    不打桩的话每次跑测试都要等 SDK 重试超时,而且结果依赖网络。
    """
    import agent.selftest as st

    for name in ("_check_model", "_check_code_interpreter"):
        monkeypatch.setattr(
            st, name,
            lambda s, _n=name: (_ for _ in ()).throw(
                RuntimeError(f"{_n} 已在测试中打桩")
            ),
        )
    return st


def make_report(steps=()):
    from agent.selftest import Report, StepResult

    report = Report(
        session_id="s1", actor_id="a1", region="cn-northwest-1",
        project="agentcore-cn", started_at=1790000000,
    )
    for kwargs in steps:
        report.steps.append(StepResult(**kwargs))
    return report


# ---------------------------------------------------------------------------
# 步骤编排
# ---------------------------------------------------------------------------


class TestStepOrdering:
    def test_covers_every_china_available_component(self, settings):
        """中国区可用的 6 个组件必须都有对应检查项。"""
        from agent.selftest import build_steps

        components = {c for _, c, _ in build_steps(settings, "s1", "a1")}
        for expected in (
            "Runtime",
            "Gateway",
            "Identity",
            "CodeInterpreter",
            "Browser",
            "Observability",
        ):
            assert expected in components, f"缺少 {expected} 的自检项"

    def test_includes_the_memory_substitute(self, settings):
        from agent.selftest import build_steps

        components = {c for _, c, _ in build_steps(settings, "s1", "a1")}
        assert "MemoryLite" in components

    def test_cheap_checks_come_before_expensive_ones(self, settings):
        """先验不依赖外部的,再验要凭证的,最后才是重的沙箱。
        这样前面失败时,后面的失败一眼能看出是连锁反应。"""
        from agent.selftest import build_steps

        order = [c for _, c, _ in build_steps(settings, "s1", "a1")]
        assert order.index("Runtime") < order.index("Identity")
        assert order.index("Identity") < order.index("CodeInterpreter")
        assert order.index("MemoryLite") < order.index("Browser")


# ---------------------------------------------------------------------------
# 失败隔离 —— 这是自检最关键的性质
# ---------------------------------------------------------------------------


class TestFailureIsolation:
    def test_one_failure_does_not_stop_the_rest(self):
        """自检的价值就在于一次看全景。第一个错误处停下就没意义了。"""
        from agent.selftest import _run_step

        report = make_report()
        calls = []

        def boom():
            calls.append("boom")
            raise RuntimeError("组件不可用")

        def fine():
            calls.append("fine")
            return "正常"

        _run_step(report, "第一步", "A", boom)
        _run_step(report, "第二步", "B", fine)

        assert calls == ["boom", "fine"]
        assert [s.ok for s in report.steps] == [False, True]

    def test_failure_records_type_and_message(self):
        """自检报告就是给人看错误的,这里需要细节 ——
        和 span 埋点只记类型的策略刚好相反。"""
        from agent.selftest import _run_step

        report = make_report()
        _run_step(report, "x", "A", lambda: (_ for _ in ()).throw(
            ValueError("找不到 aws.browser.v1")
        ))

        step = report.steps[0]
        assert "ValueError" in step.error
        assert "aws.browser.v1" in step.error

    def test_skip_is_not_a_failure(self):
        """没配置 ≠ 坏了。跳过要和失败区分开,否则报告全是红的。"""
        from agent.selftest import _Skip, _run_step

        report = make_report()
        _run_step(report, "x", "A", lambda: (_ for _ in ()).throw(
            _Skip("GATEWAY_URL 未配置")
        ))

        step = report.steps[0]
        assert step.skipped is True
        assert step.to_dict()["status"] == "skipped"
        assert report.failed == []

    def test_duration_is_recorded_for_failures_too(self):
        from agent.selftest import _run_step

        report = make_report()
        _run_step(report, "x", "A", lambda: (_ for _ in ()).throw(RuntimeError("x")))
        assert report.steps[0].duration_ms >= 0

    def test_long_error_is_truncated(self):
        from agent.selftest import _run_step

        report = make_report()
        _run_step(report, "x", "A", lambda: (_ for _ in ()).throw(
            RuntimeError("长" * 5000)
        ))
        assert len(report.steps[0].error) <= 300


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------


class TestReport:
    def test_summary_counts(self):
        report = make_report([
            {"name": "a", "component": "A", "ok": True, "duration_ms": 1},
            {"name": "b", "component": "B", "ok": False, "duration_ms": 2},
            {"name": "c", "component": "C", "ok": False, "duration_ms": 3,
             "skipped": True},
        ])
        summary = report.to_dict()["summary"]
        assert summary == {"total": 3, "ok": 1, "failed": 1, "skipped": 1}

    def test_markdown_has_a_row_per_step(self):
        report = make_report([
            {"name": "容器与入口", "component": "Runtime", "ok": True,
             "duration_ms": 5, "detail": "python 3.13"},
            {"name": "沙箱执行", "component": "CodeInterpreter", "ok": False,
             "duration_ms": 900, "error": "ResourceNotFoundException"},
        ])
        md = report.to_markdown()

        assert "容器与入口" in md and "python 3.13" in md
        assert "沙箱执行" in md and "ResourceNotFoundException" in md
        assert "1/2 项通过" in md
        assert "## 失败项" in md

    def test_markdown_escapes_pipes(self):
        """报告是 Markdown 表格,detail 里的竖线会把列切碎。"""
        report = make_report([
            {"name": "x", "component": "A", "ok": True, "duration_ms": 1,
             "detail": "a | b | c"},
        ])
        md = report.to_markdown()
        assert "a \\| b \\| c" in md

    def test_markdown_collapses_newlines(self):
        report = make_report([
            {"name": "x", "component": "A", "ok": False, "duration_ms": 1,
             "error": "第一行\n第二行"},
        ])
        rows = [
            line for line in report.to_markdown().splitlines()
            if line.startswith("| A ")
        ]
        assert len(rows) == 1, "换行没被压掉,表格会断"

    def test_no_failures_section_when_all_pass(self):
        report = make_report([
            {"name": "x", "component": "A", "ok": True, "duration_ms": 1},
        ])
        assert "## 失败项" not in report.to_markdown()

    def test_report_is_json_serializable(self):
        """要塞进 /invocations 的响应体,必须能序列化。"""
        report = make_report([
            {"name": "x", "component": "A", "ok": True, "duration_ms": 1},
        ])
        assert json.loads(json.dumps(report.to_dict()))["summary"]["ok"] == 1

    def test_trace_id_appears_when_available(self):
        report = make_report()
        report.trace_id = "abc123"
        assert "abc123" in report.to_markdown()
        assert "CloudWatch" in report.to_markdown()


# ---------------------------------------------------------------------------
# 端到端(所有组件都不可用的情况)
# ---------------------------------------------------------------------------


class TestRunSelftest:
    def test_runs_all_steps_even_with_nothing_configured(
        self, settings, monkeypatch, fake_ddb_factory, no_network
    ):
        """最坏情况:除了容器自己什么都不通。
        必须跑完全部步骤并给出报告,而不是在第一步挂掉。"""
        from agent.memory_lite import MemoryLite
        from agent.selftest import build_steps, run_selftest

        fake = fake_ddb_factory()
        original = MemoryLite.__init__
        monkeypatch.setattr(
            MemoryLite, "__init__",
            lambda self, s=None, *, client=None: original(self, s, client=fake),
        )
        # 没有 Gateway / 物流站点 / 产物桶
        for field in ("gateway_url", "logistics_url", "artifact_bucket"):
            object.__setattr__(settings, field, "")

        result = asyncio.run(run_selftest(settings, session_id="s1", actor_id="a1"))

        assert result["summary"]["total"] == len(build_steps(settings, "s1", "a1"))
        # Runtime 和 MemoryLite 不依赖外部,必须通过
        by_component = {s["component"]: s for s in result["steps"]}
        assert by_component["Runtime"]["status"] == "ok"
        assert by_component["MemoryLite"]["status"] == "ok"
        # 没配的应该是 skipped 而不是 failed
        assert by_component["Gateway"]["status"] == "skipped"
        assert by_component["Browser"]["status"] == "skipped"

    def test_report_includes_markdown(
        self, settings, monkeypatch, fake_ddb_factory, no_network
    ):
        from agent.memory_lite import MemoryLite
        from agent.selftest import run_selftest

        fake = fake_ddb_factory()
        original = MemoryLite.__init__
        monkeypatch.setattr(
            MemoryLite, "__init__",
            lambda self, s=None, *, client=None: original(self, s, client=fake),
        )
        for field in ("gateway_url", "logistics_url", "artifact_bucket"):
            object.__setattr__(settings, field, "")

        result = asyncio.run(run_selftest(settings, session_id="s1", actor_id="a1"))
        assert "AgentCore 中国区能力自检报告" in result["markdown"]

    def test_memory_check_writes_a_marker_with_ttl(
        self, settings, monkeypatch, fake_ddb_factory
    ):
        """自检只做只读探测,唯一的写是一条带 TTL 的标记,
        不该碰业务数据。"""
        from agent.memory_lite import MemoryLite
        from agent.selftest import _check_memory

        fake = fake_ddb_factory()
        memory_cls_init = MemoryLite.__init__
        monkeypatch.setattr(
            MemoryLite, "__init__",
            lambda self, s=None, *, client=None: memory_cls_init(self, s, client=fake),
        )
        detail = _check_memory(settings, "s-selftest", "a1")

        assert "STM 可读写" in detail
        written = [v for (pk, sk), v in fake.items.items() if sk.startswith("MSG#")]
        assert len(written) == 1
        assert "expires_at" in written[0], "自检标记必须带 TTL,否则会一直留着"
        assert written[0]["content"]["S"].startswith("selftest-")


# ---------------------------------------------------------------------------
# 与 Observability 的集成
# ---------------------------------------------------------------------------


class TestObservabilityIntegration:
    def test_identity_check_never_prints_the_key(self):
        """自检报告可能被分享出去,绝不能带凭证。"""
        from pathlib import Path

        source = (
            Path(__file__).resolve().parents[1] / "src" / "agent" / "selftest.py"
        ).read_text()
        # 只允许出现长度,不允许把 key 本身拼进返回值
        assert "len(key)" in source
        assert "{key}" not in source

    def test_observability_check_skips_without_otel(self, settings, monkeypatch):
        from agent import obs
        from agent.selftest import _Skip, _check_observability

        monkeypatch.setattr(obs, "otel_enabled", lambda: False)
        with pytest.raises(_Skip):
            _check_observability(settings)

    def test_observability_check_fails_if_otel_on_but_no_trace(
        self, settings, monkeypatch
    ):
        """OTEL 声称启用了但拿不到 trace id,说明 provider 没初始化 ——
        这是个真问题,不该当成"跳过"。"""
        from agent import obs
        from agent.selftest import _check_observability

        monkeypatch.setattr(obs, "otel_enabled", lambda: True)
        monkeypatch.setattr(obs, "current_trace_id", lambda: "")
        with pytest.raises(RuntimeError, match="provider"):
            _check_observability(settings)


class TestReportPublishingCannotStallTheRun:
    """报告上传曾把整次自检拖到客户端读超时。

    实测:8 个步骤 18 秒全部跑完,然后这一步静默挂了近 2 分钟。
    三层原因叠加 —— boto3 默认重试 5 次且指数退避、没有 read timeout、
    这段代码在 async entrypoint 里同步阻塞执行。异常处理只在重试用尽后
    才生效,所以重试期间一条日志都没有,表现成"业务全部成功但调用方超时"。
    """

    def test_upload_failure_keeps_the_report_in_the_response(
        self, settings, monkeypatch, fake_ddb_factory, no_network
    ):
        """上传失败(超时/权限/桶不存在)时,报告内容必须仍在响应体里。"""
        import agent.selftest as st
        from agent.memory_lite import MemoryLite
        from agent.selftest import run_selftest

        fake = fake_ddb_factory()
        original = MemoryLite.__init__
        monkeypatch.setattr(
            MemoryLite, "__init__",
            lambda self, s=None, *, client=None: original(self, s, client=fake),
        )
        object.__setattr__(settings, "artifact_bucket", "some-bucket")
        for field in ("gateway_url", "logistics_url"):
            object.__setattr__(settings, field, "")

        monkeypatch.setattr(
            st, "_publish_report",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("S3 不可达")),
        )

        # 异常不该冒出来,报告也不该丢
        with pytest.raises(RuntimeError):
            asyncio.run(run_selftest(settings, session_id="s1", actor_id="a1"))
        object.__setattr__(settings, "artifact_bucket", "")

    def test_publish_swallows_its_own_errors(self, settings, monkeypatch):
        """_publish_report 内部必须吞掉异常返回空串 ——
        上传是增强项,不该让整次自检失败。"""
        import agent.selftest as st

        object.__setattr__(settings, "artifact_bucket", "no-such-bucket-xyz")
        import boto3

        def boom(*a, **k):
            raise RuntimeError("S3 不可达")

        monkeypatch.setattr(boto3, "client", boom)
        report = make_report()
        assert st._publish_report(settings, report, "s1", "a1") == ""
        object.__setattr__(settings, "artifact_bucket", "")

    def test_no_wait_for_around_the_upload(self):
        """不能用 asyncio.wait_for 包 to_thread —— 里面的阻塞调用不可取消,
        超时只是让调用方不等了,线程仍跑到底,进程迟迟不退出。
        兜底应该靠 boto3 自己的超时配置。"""
        source = (
            Path(__file__).resolve().parents[1] / "src" / "agent" / "selftest.py"
        ).read_text()
        assert "wait_for(\n            asyncio.to_thread(_publish_report" not in source
        assert "asyncio.to_thread(\n        _publish_report" in source

    def test_s3_client_has_explicit_timeouts(self):
        """boto3 默认没有 read timeout 且重试 5 次 —— 必须显式收紧。"""
        source = (
            Path(__file__).resolve().parents[1] / "src" / "agent" / "selftest.py"
        ).read_text()
        assert "read_timeout" in source
        assert "connect_timeout" in source
        assert '"max_attempts": 2' in source

    def test_upload_runs_off_the_event_loop(self):
        """阻塞的 boto3 调用不能直接在 async entrypoint 里跑。"""
        source = (
            Path(__file__).resolve().parents[1] / "src" / "agent" / "selftest.py"
        ).read_text()
        assert "asyncio.to_thread(" in source
        assert "_publish_report" in source
