"""MemoryLite 的专项测试。

中国区没有 AgentCore Memory,这一整块是自己实现的,所以边界条件必须自己兜住:
TTL 延迟、序号冲突、窗口切割、DynamoDB 单条 400KB 上限、Query 1MB 分页。
"""

from __future__ import annotations

import time

import pytest
from botocore.exceptions import ClientError


@pytest.fixture
def memory(fake_ddb_factory):
    """返回 (MemoryLite 实例, 假 DDB)。"""
    from agent.config import get_settings
    from agent.memory_lite import MemoryLite

    fake = fake_ddb_factory()
    return MemoryLite(get_settings(), client=fake), fake


def contents(turns) -> list[str]:
    return [t.content for t in turns]


# ---------------------------------------------------------------------------
# 短期记忆:顺序与隔离
# ---------------------------------------------------------------------------


class TestShortTermOrdering:
    def test_history_comes_back_in_chronological_order(self, memory):
        mem, _ = memory
        for i in range(5):
            mem.append_turn("s1", "user", f"问题{i}")

        assert contents(mem.load_history("s1")) == [f"问题{i}" for i in range(5)]

    def test_sequence_numbers_are_zero_padded(self, memory):
        """字典序必须等于数值序,否则第 10 条会排到第 2 条前面。"""
        mem, fake = memory
        for i in range(12):
            mem.append_turn("s1", "user", f"m{i}")

        sks = sorted(sk for (pk, sk) in fake.items if pk == "SESSION#s1" and sk != "SUMMARY")
        numbers = [int(sk.removeprefix("MSG#")) for sk in sks]
        assert numbers == list(range(12)), "字典序和数值序不一致"

    def test_timestamp_based_keys_would_have_collided(self, memory):
        """说明为什么用序号而不是时间戳:同一秒写多条不能撞键。"""
        mem, fake = memory
        for i in range(4):
            mem.append_turn("s1", "user", f"同一秒的第{i}条")

        assert len(mem.load_history("s1")) == 4

    def test_roles_are_preserved(self, memory):
        mem, _ = memory
        mem.append_turn("s1", "user", "问")
        mem.append_turn("s1", "assistant", "答")

        assert [t.role for t in mem.load_history("s1")] == ["user", "assistant"]


class TestSessionIsolation:
    def test_sessions_do_not_leak_into_each_other(self, memory):
        """这就是 Runtime "会话隔离" 能力的落点。"""
        mem, _ = memory
        mem.append_turn("s1", "user", "甲的问题")
        mem.append_turn("s2", "user", "乙的问题")

        assert contents(mem.load_history("s1")) == ["甲的问题"]
        assert contents(mem.load_history("s2")) == ["乙的问题"]

    def test_empty_session_id_is_a_noop_not_a_crash(self, memory):
        mem, fake = memory
        mem.append_turn("", "user", "没有会话号")

        assert fake.items == {}
        assert mem.load_history("") == []

    def test_unknown_session_returns_empty(self, memory):
        mem, _ = memory
        assert mem.load_history("never-seen") == []


# ---------------------------------------------------------------------------
# 窗口
# ---------------------------------------------------------------------------


class TestWindow:
    def test_window_counts_turns_not_messages(self, memory):
        """stm_max_turns 是"轮"(一问一答),所以底层要取 2 倍条数。
        按条数理解会让实际窗口只有一半。"""
        mem, _ = memory
        for i in range(10):
            mem.append_turn("s1", "user", f"问{i}")
            mem.append_turn("s1", "assistant", f"答{i}")

        turns = mem.load_history("s1", max_turns=3)
        assert len(turns) == 6

    def test_window_keeps_the_most_recent(self, memory):
        mem, _ = memory
        for i in range(10):
            mem.append_turn("s1", "user", f"问{i}")

        turns = mem.load_history("s1", max_turns=2)
        assert contents(turns) == ["问6", "问7", "问8", "问9"]

    def test_history_never_starts_with_assistant(self, memory):
        """窗口边界可能正好切在 assistant 消息上。
        部分 OpenAI 兼容端点收到以 assistant 开头的历史会直接 400。"""
        mem, _ = memory
        # 造一个奇数条的历史,让窗口边界落在 assistant 上
        mem.append_turn("s1", "user", "问0")
        mem.append_turn("s1", "assistant", "答0")
        mem.append_turn("s1", "assistant", "追加的答0")
        mem.append_turn("s1", "user", "问1")
        mem.append_turn("s1", "assistant", "答1")

        turns = mem.load_history("s1", max_turns=2)
        assert turns, "窗口不该被清空"
        assert turns[0].role == "user", f"历史以 {turns[0].role} 开头"

    def test_all_assistant_history_yields_empty(self, memory):
        """极端情况:窗口里全是 assistant 消息,只能返回空。"""
        mem, _ = memory
        mem.append_turn("s1", "assistant", "只有答")

        assert mem.load_history("s1") == []

    def test_strands_message_format(self, memory):
        mem, _ = memory
        mem.append_turn("s1", "user", "你好")
        mem.append_turn("s1", "assistant", "在")

        assert mem.as_strands_messages("s1") == [
            {"role": "user", "content": [{"text": "你好"}]},
            {"role": "assistant", "content": [{"text": "在"}]},
        ]


# ---------------------------------------------------------------------------
# TTL
# ---------------------------------------------------------------------------


class TestTtl:
    def test_ttl_is_written(self, memory):
        from agent.config import get_settings

        mem, fake = memory
        before = int(time.time())
        mem.append_turn("s1", "user", "x")

        item = next(v for (pk, sk), v in fake.items.items() if sk.startswith("MSG#"))
        expires = int(item["expires_at"]["N"])
        assert expires >= before + get_settings().stm_ttl_seconds

    def test_expired_items_are_filtered_even_if_ttl_sweep_lagged(self, memory):
        """DynamoDB 的 TTL 清理最长可能延迟 48 小时。
        不自己再过滤一次,用户会看到早该过期的历史。"""
        mem, fake = memory
        mem.append_turn("s1", "user", "过期的")
        mem.append_turn("s1", "user", "新鲜的")

        keys = sorted(k for k in fake.items if k[1].startswith("MSG#"))
        fake.items[keys[0]]["expires_at"] = {"N": str(int(time.time()) - 1)}

        assert contents(mem.load_history("s1")) == ["新鲜的"]

    def test_expiring_the_leading_user_message_still_yields_valid_history(self, memory):
        """过期过滤可能把开头的 user 消息滤掉,剩下 assistant 开头 ——
        必须继续修正,不能只在切窗口时修一次。"""
        mem, fake = memory
        mem.append_turn("s1", "user", "旧问")
        mem.append_turn("s1", "assistant", "旧答")
        mem.append_turn("s1", "user", "新问")

        keys = sorted(k for k in fake.items if k[1].startswith("MSG#"))
        fake.items[keys[0]]["expires_at"] = {"N": str(int(time.time()) - 1)}

        turns = mem.load_history("s1")
        assert turns[0].role == "user"
        assert contents(turns) == ["新问"]


# ---------------------------------------------------------------------------
# 序号冲突与长度上限
# ---------------------------------------------------------------------------


class TestWriteRobustness:
    def test_sequence_conflict_is_retried(self, memory, monkeypatch):
        mem, fake = memory
        original = fake.put_item
        calls = {"n": 0}

        def flaky(**kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ClientError(
                    {"Error": {"Code": "ConditionalCheckFailedException", "Message": "x"}},
                    "PutItem",
                )
            return original(**kwargs)

        monkeypatch.setattr(fake, "put_item", flaky)
        mem.append_turn("s1", "user", "冲突后应该写进去")

        assert contents(mem.load_history("s1")) == ["冲突后应该写进去"]
        assert calls["n"] == 2

    def test_persistent_conflict_gives_up_without_stack_overflow(self, memory, monkeypatch):
        """早先的实现是递归重试,持续冲突会一路递归到栈溢出。
        现在是有界重试,用尽就放弃这一条。"""
        from agent.memory_lite import _SEQ_CONFLICT_RETRIES

        mem, fake = memory
        calls = {"n": 0}

        def always_conflict(**kwargs):
            calls["n"] += 1
            raise ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException", "Message": "x"}},
                "PutItem",
            )

        monkeypatch.setattr(fake, "put_item", always_conflict)
        mem.append_turn("s1", "user", "永远冲突")  # 不该抛,也不该爆栈

        assert calls["n"] == _SEQ_CONFLICT_RETRIES

    def test_other_client_errors_are_not_swallowed(self, memory, monkeypatch):
        """只有条件检查失败才重试;权限/限流之类的必须抛出去。"""
        mem, fake = memory

        def denied(**kwargs):
            raise ClientError(
                {"Error": {"Code": "AccessDeniedException", "Message": "nope"}}, "PutItem"
            )

        monkeypatch.setattr(fake, "put_item", denied)
        with pytest.raises(ClientError):
            mem.append_turn("s1", "user", "x")

    def test_oversized_content_is_truncated_not_rejected(self, memory):
        """DynamoDB 单条上限 400KB。不截断的话整次写入 ValidationException,
        这一轮的历史就整条丢了。"""
        from agent.memory_lite import MAX_CONTENT_CHARS

        mem, _ = memory
        mem.append_turn("s1", "user", "长" * (MAX_CONTENT_CHARS + 5000))

        stored = mem.load_history("s1")[0].content
        assert len(stored) == MAX_CONTENT_CHARS
        assert stored.endswith("[已截断]")

    def test_content_at_the_limit_is_untouched(self, memory):
        from agent.memory_lite import MAX_CONTENT_CHARS

        mem, _ = memory
        exact = "x" * MAX_CONTENT_CHARS
        mem.append_turn("s1", "user", exact)

        assert mem.load_history("s1")[0].content == exact

    def test_none_content_does_not_crash(self, memory):
        mem, _ = memory
        mem.append_turn("s1", "user", None)  # type: ignore[arg-type]
        assert mem.load_history("s1")[0].content == ""


# ---------------------------------------------------------------------------
# 长期记忆
# ---------------------------------------------------------------------------


class TestLongTermMemory:
    def test_roundtrip(self, memory):
        mem, _ = memory
        mem.put_fact("a1", "notify_channel", "短信")

        assert mem.get_facts("a1") == {"notify_channel": "短信"}

    def test_facts_survive_across_sessions(self, memory):
        """长期记忆是按 actor 存的,和 session 无关 —— 这是它和 STM 的区别。"""
        mem, _ = memory
        mem.put_fact("a1", "pref", "值")
        mem.append_turn("s1", "user", "第一次会话")
        mem.append_turn("s2", "user", "第二次会话")

        assert mem.get_facts("a1") == {"pref": "值"}

    def test_actors_are_isolated(self, memory):
        mem, _ = memory
        mem.put_fact("a1", "secret", "甲的")
        mem.put_fact("a2", "secret", "乙的")

        assert mem.get_facts("a1") == {"secret": "甲的"}
        assert mem.get_facts("a2") == {"secret": "乙的"}

    def test_overwrite_same_key(self, memory):
        mem, _ = memory
        mem.put_fact("a1", "k", "旧值")
        mem.put_fact("a1", "k", "新值")

        assert mem.get_facts("a1") == {"k": "新值"}

    def test_delete_fact(self, memory):
        mem, _ = memory
        mem.put_fact("a1", "k1", "v1")
        mem.put_fact("a1", "k2", "v2")
        mem.delete_fact("a1", "k1")

        assert mem.get_facts("a1") == {"k2": "v2"}

    def test_facts_have_no_ttl(self, memory):
        """长期记忆不该过期 —— 写了 expires_at 就会被 DynamoDB 清掉。"""
        mem, fake = memory
        mem.put_fact("a1", "k", "v")

        item = fake.items[("ACTOR#a1", "FACT#k")]
        assert "expires_at" not in item

    def test_empty_inputs_are_noops(self, memory):
        mem, fake = memory
        mem.put_fact("", "k", "v")
        mem.put_fact("a1", "", "v")
        mem.delete_fact("", "k")

        assert fake.items == {}
        assert mem.get_facts("") == {}

    def test_long_value_is_truncated(self, memory):
        from agent.memory_lite import MAX_FACT_CHARS

        mem, _ = memory
        mem.put_fact("a1", "k", "长" * (MAX_FACT_CHARS + 100))

        assert len(mem.get_facts("a1")["k"]) == MAX_FACT_CHARS

    def test_fact_count_is_capped(self, memory):
        """不设上限的话模型可以把整段对话当"偏好"无限写进去。"""
        from agent.memory_lite import MAX_FACTS_PER_ACTOR, MemoryLimitExceeded

        mem, _ = memory
        for i in range(MAX_FACTS_PER_ACTOR):
            mem.put_fact("a1", f"k{i}", "v")

        with pytest.raises(MemoryLimitExceeded, match="上限"):
            mem.put_fact("a1", "one-too-many", "v")

    def test_overwriting_is_allowed_at_the_cap(self, memory):
        """到上限之后仍然可以覆盖已有的 key,只是不能新增。"""
        from agent.memory_lite import MAX_FACTS_PER_ACTOR

        mem, _ = memory
        for i in range(MAX_FACTS_PER_ACTOR):
            mem.put_fact("a1", f"k{i}", "旧")

        mem.put_fact("a1", "k0", "新")  # 不该抛
        assert mem.get_facts("a1")["k0"] == "新"

    def test_get_facts_pages_through_everything(self, memory):
        """Query 单次最多返回 1MB。不翻页会静默丢数据 ——
        症状是"用户偏好时有时无",极难查。"""
        mem, fake = memory
        for i in range(12):
            mem.put_fact("a1", f"k{i:02d}", f"v{i}")

        # 强制每页只给 3 条,逼调用方翻页
        fake.page_size = 3
        facts = mem.get_facts("a1")

        assert len(facts) == 12, f"只拿到 {len(facts)} 条,说明没翻页"


# ---------------------------------------------------------------------------
# 会话摘要
# ---------------------------------------------------------------------------


class TestSummary:
    def test_roundtrip(self, memory):
        mem, _ = memory
        mem.set_summary("s1", "用户在问 ORD-1024 的赔付", covered_upto=10)

        assert mem.get_summary("s1") == ("用户在问 ORD-1024 的赔付", 10)

    def test_no_summary_yields_empty(self, memory):
        mem, _ = memory
        assert mem.get_summary("s1") == ("", 0)

    def test_blank_summary_is_not_stored(self, memory):
        mem, fake = memory
        mem.set_summary("s1", "   ", covered_upto=5)

        assert ("SESSION#s1", "SUMMARY") not in fake.items

    def test_summary_is_truncated(self, memory):
        from agent.memory_lite import MAX_SUMMARY_CHARS

        mem, _ = memory
        mem.set_summary("s1", "长" * (MAX_SUMMARY_CHARS + 500), covered_upto=1)

        text, _ = mem.get_summary("s1")
        assert len(text) == MAX_SUMMARY_CHARS

    def test_expired_summary_is_ignored(self, memory):
        mem, fake = memory
        mem.set_summary("s1", "旧摘要", covered_upto=1)
        fake.items[("SESSION#s1", "SUMMARY")]["expires_at"] = {
            "N": str(int(time.time()) - 1)
        }

        assert mem.get_summary("s1") == ("", 0)

    def test_summary_does_not_pollute_message_history(self, memory):
        """SUMMARY 和 MSG# 在同一个分区里,查历史时不能把它也捞出来。"""
        mem, _ = memory
        mem.append_turn("s1", "user", "问题")
        mem.set_summary("s1", "摘要内容", covered_upto=0)

        assert contents(mem.load_history("s1")) == ["问题"]
        assert mem.count_messages("s1") == 1


class TestNeedsSummary:
    def test_short_session_does_not_need_summary(self, memory):
        mem, _ = memory
        for i in range(4):
            mem.append_turn("s1", "user", f"m{i}")

        assert mem.needs_summary("s1") is False

    def test_long_session_needs_summary(self, memory):
        from agent.config import get_settings

        mem, _ = memory
        window = get_settings().stm_max_turns * 2
        for i in range(window + 6):
            mem.append_turn("s1", "user", f"m{i}")

        assert mem.needs_summary("s1") is True

    def test_fresh_summary_suppresses_resummarizing(self, memory):
        """已经摘要过的部分不该重复花钱再摘一遍。"""
        from agent.config import get_settings

        mem, _ = memory
        window = get_settings().stm_max_turns * 2
        total = window + 6
        for i in range(total):
            mem.append_turn("s1", "user", f"m{i}")
        mem.set_summary("s1", "已覆盖", covered_upto=total - window)

        assert mem.needs_summary("s1") is False

    def test_stale_summary_triggers_resummarizing(self, memory):
        from agent.config import get_settings

        mem, _ = memory
        window = get_settings().stm_max_turns * 2
        for i in range(window + 10):
            mem.append_turn("s1", "user", f"m{i}")
        mem.set_summary("s1", "只覆盖了前 2 条", covered_upto=2)

        assert mem.needs_summary("s1") is True

    def test_messages_outside_window_are_the_older_ones(self, memory):
        from agent.config import get_settings

        mem, _ = memory
        window = get_settings().stm_max_turns * 2
        total = window + 3
        for i in range(total):
            mem.append_turn("s1", "user", f"m{i}")

        older = mem.messages_outside_window("s1")
        assert contents(older) == ["m0", "m1", "m2"]

    def test_no_messages_outside_a_short_session(self, memory):
        mem, _ = memory
        mem.append_turn("s1", "user", "m0")
        assert mem.messages_outside_window("s1") == []
