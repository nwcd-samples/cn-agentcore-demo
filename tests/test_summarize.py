"""会话摘要的测试。

摘要要解决的问题:对话窗口是固定的,超出窗口的历史直接丢掉会让长会话
突然"失忆" —— 用户前面报过的运单号、已经承诺过的赔付金额都没了。

摘要涉及一次额外的模型调用,所以两件事必须成立:
  1. 不该摘要的时候绝不调模型(省钱)
  2. 摘要失败绝不能拖垮正常对话(它只是增强项)
"""

from __future__ import annotations

import pytest


@pytest.fixture
def memory(fake_ddb_factory):
    from agent.config import get_settings
    from agent.memory_lite import MemoryLite

    fake = fake_ddb_factory()
    return MemoryLite(get_settings(), client=fake), fake


@pytest.fixture
def window():
    from agent.config import get_settings

    return get_settings().stm_max_turns * 2


def fill(mem, session_id: str, count: int) -> None:
    for i in range(count):
        role = "user" if i % 2 == 0 else "assistant"
        mem.append_turn(session_id, role, f"消息{i}")


class RecordingSummarizer:
    def __init__(self, result: str = "用户在问 ORD-1024 的超期赔付,已承诺 194.85 元"):
        self.calls: list[list] = []
        self.result = result

    def __call__(self, turns, settings=None) -> str:
        self.calls.append(list(turns))
        return self.result


# ---------------------------------------------------------------------------
# 何时该摘要
# ---------------------------------------------------------------------------


class TestWhenToSummarize:
    def test_short_session_never_calls_the_model(self, memory, window):
        """省钱:没超窗口就不该有任何模型调用。"""
        from agent.summarize import maybe_summarize

        mem, _ = memory
        fill(mem, "s1", 4)
        summarizer = RecordingSummarizer()

        assert maybe_summarize(mem, "s1", summarizer=summarizer) is False
        assert summarizer.calls == []

    def test_long_session_triggers_summary(self, memory, window):
        from agent.summarize import maybe_summarize

        mem, _ = memory
        fill(mem, "s1", window + 6)
        summarizer = RecordingSummarizer()

        assert maybe_summarize(mem, "s1", summarizer=summarizer) is True
        assert len(summarizer.calls) == 1

    def test_second_call_is_a_noop(self, memory, window):
        """摘要写完后 covered_upto 就跟上了,再调不该重复花钱。"""
        from agent.summarize import maybe_summarize

        mem, _ = memory
        fill(mem, "s1", window + 6)
        summarizer = RecordingSummarizer()

        maybe_summarize(mem, "s1", summarizer=summarizer)
        assert maybe_summarize(mem, "s1", summarizer=summarizer) is False
        assert len(summarizer.calls) == 1

    def test_more_turns_trigger_a_refresh(self, memory, window):
        from agent.summarize import maybe_summarize

        mem, _ = memory
        fill(mem, "s1", window + 6)
        summarizer = RecordingSummarizer()
        maybe_summarize(mem, "s1", summarizer=summarizer)

        fill(mem, "s1", 8)
        assert maybe_summarize(mem, "s1", summarizer=summarizer) is True
        assert len(summarizer.calls) == 2

    def test_empty_session_id_is_a_noop(self, memory):
        from agent.summarize import maybe_summarize

        mem, _ = memory
        summarizer = RecordingSummarizer()
        assert maybe_summarize(mem, "", summarizer=summarizer) is False
        assert summarizer.calls == []


# ---------------------------------------------------------------------------
# 摘要内容
# ---------------------------------------------------------------------------


class TestSummaryContent:
    def test_only_messages_outside_the_window_are_summarized(self, memory, window):
        """窗口内的消息本来就会原样传给模型,再摘一遍是浪费。"""
        from agent.summarize import maybe_summarize

        mem, _ = memory
        fill(mem, "s1", window + 3)
        summarizer = RecordingSummarizer()
        maybe_summarize(mem, "s1", summarizer=summarizer)

        summarized = [t.content for t in summarizer.calls[0]]
        assert summarized == ["消息0", "消息1", "消息2"]

    def test_previous_summary_is_folded_in(self, memory, window):
        """滚动摘要:上一版摘要要作为输入喂进去,
        否则第二次摘要会丢掉更早的信息。"""
        from agent.summarize import maybe_summarize

        mem, _ = memory
        fill(mem, "s1", window + 4)
        summarizer = RecordingSummarizer(result="第一版摘要")
        maybe_summarize(mem, "s1", summarizer=summarizer)

        fill(mem, "s1", 6)
        maybe_summarize(mem, "s1", summarizer=summarizer)

        second_input = [t.content for t in summarizer.calls[1]]
        assert second_input[0].startswith("[已有摘要] 第一版摘要")

    def test_summary_is_persisted_with_coverage(self, memory, window):
        from agent.summarize import maybe_summarize

        mem, _ = memory
        total = window + 5
        fill(mem, "s1", total)
        maybe_summarize(mem, "s1", summarizer=RecordingSummarizer(result="要点"))

        text, covered = mem.get_summary("s1")
        assert text == "要点"
        assert covered == total - window

    def test_render_conversation_labels_speakers(self):
        from agent.memory_lite import Turn
        from agent.summarize import render_conversation

        text = render_conversation(
            [
                Turn(role="user", content="ORD-1024 到哪了", created_at=1),
                Turn(role="assistant", content="卡在西安中转", created_at=2),
            ]
        )
        assert "用户:ORD-1024 到哪了" in text
        assert "助手:卡在西安中转" in text

    def test_render_conversation_caps_length(self):
        from agent.memory_lite import Turn
        from agent.summarize import render_conversation

        turns = [Turn(role="user", content="长" * 1000, created_at=i) for i in range(30)]
        text = render_conversation(turns)
        assert len(text) < 30 * 1000
        assert "更早的内容已在上一版摘要中" in text

    def test_prompt_asks_to_keep_the_things_that_matter(self):
        """摘要必须保留订单号/金额这类硬信息,否则后续对话会失去依据。"""
        from agent.summarize import SUMMARY_PROMPT

        for keyword in ("订单号", "运单号", "金额", "偏好"):
            assert keyword in SUMMARY_PROMPT


# ---------------------------------------------------------------------------
# 失败降级
# ---------------------------------------------------------------------------


class TestFailureIsolation:
    def test_summarizer_returning_empty_does_not_write(self, memory, window):
        from agent.summarize import maybe_summarize

        mem, _ = memory
        fill(mem, "s1", window + 4)

        assert maybe_summarize(mem, "s1", summarizer=lambda t, s=None: "") is False
        assert mem.get_summary("s1") == ("", 0)

    def test_model_failure_is_swallowed(self, memory, window, monkeypatch):
        """summarize_turns 内部吞掉模型异常并返回空串 ——
        摘要失败不该让整轮对话失败。

        注意 patch 的目标:summarize.py 里是在函数体内
        `from agent.model import build_model`,所以必须打
        **agent.model.build_model**。打 summarize_mod.build_model 不生效 ——
        那样这个测试会真的去调 DeepSeek,有真 key 时就会拿到真摘要而不是 ""。
        """
        import agent.model as model_mod
        import agent.summarize as summarize_mod
        from agent.memory_lite import Turn

        def boom(*args, **kwargs):
            raise RuntimeError("DeepSeek 超时")

        monkeypatch.setattr(model_mod, "build_model", boom)
        result = summarize_mod.summarize_turns(
            [Turn(role="user", content="x", created_at=1)]
        )
        assert result == ""

    def test_the_patch_target_is_actually_effective(self, monkeypatch):
        """反向对照:确认上面 patch 的目标真的被调用到了。

        这条存在的理由是上面那个测试曾经打错目标(patch 了
        summarize_mod.build_model),导致它在没有真 key 时"意外通过"
        (取 key 先失败了),有真 key 时才暴露出它一直在打真实 API。
        """
        import agent.model as model_mod
        import agent.summarize as summarize_mod
        from agent.memory_lite import Turn

        called = []

        def spy(*args, **kwargs):
            called.append(True)
            raise RuntimeError("stop here")

        monkeypatch.setattr(model_mod, "build_model", spy)
        summarize_mod.summarize_turns([Turn(role="user", content="x", created_at=1)])
        assert called, "patch 的目标没被调用 —— 说明 patch 位置不对,测试在打真实 API"

    def test_no_turns_yields_empty_without_calling_model(self):
        from agent.summarize import summarize_turns

        assert summarize_turns([]) == ""


# ---------------------------------------------------------------------------
# 与 system prompt 的集成
# ---------------------------------------------------------------------------


class TestPromptIntegration:
    def test_summary_is_injected_into_system_prompt(self, memory, monkeypatch):
        import agent.assembly as assembly
        from agent.memory_lite import MemoryLite

        mem, fake = memory
        mem.set_summary("s1", "用户已确认赔付 194.85 元", covered_upto=4)

        original = MemoryLite.__init__
        monkeypatch.setattr(
            MemoryLite, "__init__",
            lambda self, settings=None, *, client=None: original(
                self, settings, client=fake
            ),
        )
        prompt = assembly._build_system_prompt(MemoryLite(), "a1", "s1")

        assert "用户已确认赔付 194.85 元" in prompt
        assert assembly.SYSTEM_PROMPT in prompt

    def test_no_summary_leaves_prompt_clean(self, memory, monkeypatch):
        import agent.assembly as assembly
        from agent.memory_lite import MemoryLite

        mem, fake = memory
        original = MemoryLite.__init__
        monkeypatch.setattr(
            MemoryLite, "__init__",
            lambda self, settings=None, *, client=None: original(
                self, settings, client=fake
            ),
        )
        assert assembly._build_system_prompt(MemoryLite(), "a1", "s-none") == (
            assembly.SYSTEM_PROMPT
        )

    def test_summary_read_failure_falls_back(self, memory, monkeypatch):
        """读摘要失败只该少一段上下文,不该让对话挂掉。"""
        import agent.assembly as assembly
        from agent.memory_lite import MemoryLite

        mem, fake = memory
        original = MemoryLite.__init__
        monkeypatch.setattr(
            MemoryLite, "__init__",
            lambda self, settings=None, *, client=None: original(
                self, settings, client=fake
            ),
        )
        monkeypatch.setattr(
            MemoryLite, "get_summary",
            lambda self, sid: (_ for _ in ()).throw(RuntimeError("DDB 挂了")),
        )
        assert assembly._build_system_prompt(MemoryLite(), "a1", "s1") == (
            assembly.SYSTEM_PROMPT
        )
