"""Agent 组装的降级行为测试。

demo 是分阶段部署的:P1 做完时 Code Interpreter 和 Browser 还不存在,
Gateway 也可能还没建。这种情况下 Agent 必须照样能起来,
只是工具少几个 —— 否则前一阶段的验证会被后一阶段的缺失拖死。

这里验证的就是这个契约,以及 MCP 会话的生命周期管理。
"""

from __future__ import annotations

import pytest


@pytest.fixture
def patched_agent(monkeypatch, fake_ddb_factory):
    """只把 DynamoDB 换成假实现。

    模型刻意用真的 OpenAIModel —— 构造它不发任何网络请求,
    而 Strands 的 Agent 会读 model.stateful 之类的属性,
    用 object() 冒充会在组装阶段就炸,测不到真正想测的东西。
    """
    import agent.assembly as assembly
    from agent.memory_lite import MemoryLite

    fake_ddb = fake_ddb_factory()

    original_init = MemoryLite.__init__
    monkeypatch.setattr(
        MemoryLite,
        "__init__",
        lambda self, settings=None, *, client=None: original_init(
            self, settings, client=fake_ddb
        ),
    )
    return assembly, fake_ddb


def tool_names_of(agent) -> set[str]:
    return set(agent.tool_names)


class TestGracefulDegradation:
    def test_agent_builds_without_gateway(self, patched_agent, monkeypatch):
        """GATEWAY_URL 没设时只应少掉业务工具,不该抛异常。"""
        assembly, _ = patched_agent
        settings = assembly.get_settings()
        object.__setattr__(settings, "gateway_url", "")

        with assembly.agent_session(
            session_id="s1", actor_id="a1", settings=settings, stream=False
        ) as agent:
            names = tool_names_of(agent)

        # memory 的三个工具必须在,它只依赖 DynamoDB
        assert {"remember", "recall", "forget"} <= names

    def test_gateway_failure_does_not_break_assembly(self, patched_agent, monkeypatch):
        """Gateway 连不上(还没建、token 过期、网络不通)时,
        Agent 仍要起来,只是没有业务工具。"""
        assembly, _ = patched_agent
        settings = assembly.get_settings()
        object.__setattr__(settings, "gateway_url", "https://gw.invalid/mcp")

        import agent.tools.gateway as gateway_module

        monkeypatch.setattr(
            gateway_module,
            "load_gateway_tools",
            lambda _settings=None: (_ for _ in ()).throw(RuntimeError("Gateway 不可达")),
        )

        with assembly.agent_session(
            session_id="s1", actor_id="a1", settings=settings, stream=False
        ) as agent:
            # 业务工具没了,但 memory 工具还在,Agent 可用
            assert {"remember", "recall", "forget"} <= tool_names_of(agent)

        object.__setattr__(settings, "gateway_url", "")

    def test_memory_read_failure_falls_back_to_base_prompt(self, patched_agent, monkeypatch):
        """读长期记忆失败不该让整轮对话挂掉。"""
        assembly, _ = patched_agent
        from agent.memory_lite import MemoryLite

        monkeypatch.setattr(
            MemoryLite, "get_facts",
            lambda self, actor_id: (_ for _ in ()).throw(RuntimeError("DDB 挂了")),
        )
        memory = MemoryLite()
        prompt = assembly._build_system_prompt(memory, "a1", "s1")
        assert prompt == assembly.SYSTEM_PROMPT


class TestMcpSessionLifecycle:
    def test_gateway_client_is_stopped_on_exit(self, patched_agent, monkeypatch):
        """MCPClient 的会话必须活到 with 块结束,然后被关掉。

        这是 assembly 用上下文管理器而不是普通工厂函数的唯一原因:
        拿完 tools 就关 client 的话,工具调用会在运行时失败。
        """
        assembly, _ = patched_agent
        settings = assembly.get_settings()
        object.__setattr__(settings, "gateway_url", "https://gw.example/mcp")

        stopped: list[bool] = []

        class FakeClient:
            def stop(self, exc_type, exc_val, exc_tb):
                stopped.append(True)

        import agent.tools.gateway as gateway_module

        monkeypatch.setattr(
            gateway_module, "load_gateway_tools", lambda _s=None: (FakeClient(), [])
        )

        with assembly.agent_session(
            session_id="s1", actor_id="a1", settings=settings, stream=False
        ):
            # 会话进行中不能已经被关掉
            assert stopped == []

        assert stopped == [True], "退出 with 后必须关闭 MCP 会话"
        object.__setattr__(settings, "gateway_url", "")

    def test_client_stopped_even_if_body_raises(self, patched_agent, monkeypatch):
        assembly, _ = patched_agent
        settings = assembly.get_settings()
        object.__setattr__(settings, "gateway_url", "https://gw.example/mcp")

        stopped: list[bool] = []

        class FakeClient:
            def stop(self, *args):
                stopped.append(True)

        import agent.tools.gateway as gateway_module

        monkeypatch.setattr(
            gateway_module, "load_gateway_tools", lambda _s=None: (FakeClient(), [])
        )

        with pytest.raises(ValueError):
            with assembly.agent_session(
                session_id="s1", actor_id="a1", settings=settings, stream=False
            ):
                raise ValueError("对话中途失败")

        assert stopped == [True], "异常路径也必须关闭 MCP 会话"
        object.__setattr__(settings, "gateway_url", "")


class TestSystemPrompt:
    def test_long_term_facts_are_injected(self, patched_agent):
        assembly, _ = patched_agent
        from agent.memory_lite import MemoryLite

        memory = MemoryLite()
        memory.put_fact("a1", "notify_channel", "短信")
        memory.put_fact("a1", "contact_window", "工作日 19:00 后")

        prompt = assembly._build_system_prompt(memory, "a1", "s1")

        assert "notify_channel: 短信" in prompt
        assert "contact_window: 工作日 19:00 后" in prompt
        assert assembly.SYSTEM_PROMPT in prompt

    def test_other_actors_facts_are_not_leaked(self, patched_agent):
        assembly, _ = patched_agent
        from agent.memory_lite import MemoryLite

        memory = MemoryLite()
        memory.put_fact("a1", "secret", "甲的秘密")
        memory.put_fact("a2", "secret", "乙的秘密")

        prompt = assembly._build_system_prompt(memory, "a1", "s1")

        assert "甲的秘密" in prompt
        assert "乙的秘密" not in prompt

    def test_no_facts_yields_base_prompt(self, patched_agent):
        assembly, _ = patched_agent
        from agent.memory_lite import MemoryLite

        assert assembly._build_system_prompt(MemoryLite(), "nobody", "s-none") == assembly.SYSTEM_PROMPT
