"""Runtime 契约的本地测试。

不需要 AWS 凭证,也不需要 DeepSeek key:
  * DynamoDB 用 conftest 里的假实现
  * 模型调用被 monkeypatch 掉

验证的是 AgentCore Runtime 真正要求的东西:
  POST /invocations 各 mode 的行为、GET /ping 的忙态、会话隔离。
"""

from __future__ import annotations

import json
import time

import pytest
from starlette.testclient import TestClient


@pytest.fixture
def runtime(monkeypatch, fake_ddb_factory):
    """把 agent.main 的依赖换成假实现,返回 (TestClient, 假 DDB, 记录器)。"""
    import agent.main as main
    from agent.memory_lite import MemoryLite

    fake_ddb = fake_ddb_factory()

    # 真实 MemoryLite 的逻辑保留,只换掉 boto3 客户端
    original_init = MemoryLite.__init__

    def patched_init(self, settings=None, *, client=None):
        original_init(self, settings, client=fake_ddb)

    monkeypatch.setattr(MemoryLite, "__init__", patched_init)

    calls: list[str] = []

    async def fake_run_agent(prompt, *, session_id, actor_id):
        calls.append(prompt)
        memory = MemoryLite()
        memory.append_turn(session_id, "user", prompt)
        answer = f"[stub:{actor_id}] {prompt}"
        memory.append_turn(session_id, "assistant", answer)
        return answer

    monkeypatch.setattr(main, "_run_agent", fake_run_agent)
    main._async_results.clear()

    with TestClient(main.app) as client:
        yield client, fake_ddb, calls


class TestPing:
    def test_ping_is_healthy_when_idle(self, runtime):
        client, _, _ = runtime
        response = client.get("/ping")
        assert response.status_code == 200
        assert response.json()["status"] == "Healthy"


class TestSyncInvocation:
    def test_returns_answer_and_echoes_session(self, runtime):
        client, _, calls = runtime
        response = client.post(
            "/invocations",
            json={"prompt": "ORD-1024 到哪了"},
            headers={"X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": "sess-a"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["answer"].endswith("ORD-1024 到哪了")
        assert body["sessionId"] == "sess-a"
        assert calls == ["ORD-1024 到哪了"]

    @pytest.mark.parametrize("key", ["prompt", "input", "message", "query", "text"])
    def test_accepts_common_input_field_names(self, runtime, key):
        client, _, _ = runtime
        response = client.post("/invocations", json={key: "你好"})
        assert response.status_code == 200
        assert "answer" in response.json()

    def test_missing_prompt_is_reported_not_crashed(self, runtime):
        client, _, _ = runtime
        response = client.post("/invocations", json={})
        assert response.status_code == 200
        assert "error" in response.json()

    def test_invalid_json_is_400(self, runtime):
        client, _, _ = runtime
        response = client.post(
            "/invocations",
            content=b"{not json",
            headers={"content-type": "application/json"},
        )
        assert response.status_code == 400


class TestSessionIsolation:
    def test_two_sessions_do_not_share_history(self, runtime):
        """Runtime 的会话隔离能力落在 MemoryLite 的分区键上。"""
        client, _, _ = runtime
        from agent.memory_lite import MemoryLite

        for session, text in (("sess-a", "甲的问题"), ("sess-b", "乙的问题")):
            client.post(
                "/invocations",
                json={"prompt": text},
                headers={"X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": session},
            )

        memory = MemoryLite()
        a = [t.content for t in memory.load_history("sess-a")]
        b = [t.content for t in memory.load_history("sess-b")]

        assert "甲的问题" in a and "乙的问题" not in a
        assert "乙的问题" in b and "甲的问题" not in b

    def test_history_keeps_chronological_order(self, runtime):
        client, _, _ = runtime
        from agent.memory_lite import MemoryLite

        for i in range(4):
            client.post(
                "/invocations",
                json={"prompt": f"第{i}问"},
                headers={"X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": "sess-order"},
            )

        turns = MemoryLite().load_history("sess-order", max_turns=100)
        user_turns = [t.content for t in turns if t.role == "user"]
        assert user_turns == ["第0问", "第1问", "第2问", "第3问"]

    def test_anonymous_actor_used_without_bearer_token(self, runtime):
        client, _, _ = runtime
        body = client.post("/invocations", json={"prompt": "x"}).json()
        assert body["actorId"] == "anonymous"

    def test_actor_id_comes_from_jwt(self, runtime):
        """自建 IdP 在 password grant 里写了 actor_id,容器要用它做数据分区。"""
        import base64

        client, _, _ = runtime
        claims = base64.urlsafe_b64encode(
            json.dumps({"sub": "actor-demo-001", "actor_id": "actor-demo-001",
                        "username": "demo-user", "scope": "agent:invoke"}).encode()
        ).rstrip(b"=").decode()
        token = f"eyJhbGciOiJSUzI1NiJ9.{claims}.sig"

        body = client.post(
            "/invocations",
            json={"prompt": "x"},
            headers={"Authorization": f"Bearer {token}"},
        ).json()
        assert body["actorId"] == "actor-demo-001"


class TestStreaming:
    def test_stream_mode_returns_sse(self, runtime, monkeypatch):
        client, _, _ = runtime
        import agent.main as main

        async def fake_stream(prompt, *, session_id, actor_id):
            yield {"type": "tool", "name": "get_order"}
            for piece in ("订单", "已", "签收"):
                yield {"type": "text", "delta": piece}
            yield {"type": "done", "sessionId": session_id}

        monkeypatch.setattr(main, "_stream_agent", fake_stream)

        with client.stream(
            "POST", "/invocations", json={"mode": "stream", "prompt": "查订单"}
        ) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            events = [
                json.loads(line[len("data: "):])
                for line in response.iter_lines()
                if line.startswith("data: ")
            ]

        assert events[0] == {"type": "tool", "name": "get_order"}
        assert "".join(e["delta"] for e in events if e["type"] == "text") == "订单已签收"
        assert events[-1]["type"] == "done"


class TestAsyncTask:
    def test_async_mode_returns_task_id_then_completes(self, runtime):
        client, _, _ = runtime
        import agent.main as main

        accepted = client.post(
            "/invocations", json={"mode": "async", "prompt": "跑个长任务"}
        ).json()
        assert accepted["status"] == "running"
        task_id = accepted["taskId"]

        # 后台任务在同一个事件循环里,轮询直到结束
        for _ in range(100):
            status = client.post(
                "/invocations", json={"mode": "status", "taskId": task_id}
            ).json()
            if status["status"] != "running":
                break
        assert status["status"] == "succeeded"
        assert "跑个长任务" in status["answer"]
        assert status["finishedAt"] >= status["startedAt"]

    def test_unknown_task_id(self, runtime):
        client, _, _ = runtime
        status = client.post(
            "/invocations", json={"mode": "status", "taskId": "nope"}
        ).json()
        assert status["status"] == "unknown"

    def test_ping_reports_busy_while_task_runs(self, runtime, monkeypatch):
        """@app.async_task 期间 /ping 必须是 HealthyBusy,
        否则 Runtime 会以为实例空闲把它回收掉。"""
        import asyncio
        import threading

        import agent.main as main

        client, _, _ = runtime
        # 用 threading.Event 而不是 asyncio.Event:
        # handler 跑在框架的 worker loop 线程上,而测试在主线程,
        # asyncio.Event.set() 不是线程安全的,唤不醒对端。
        release = threading.Event()

        async def slow_run(prompt, *, session_id, actor_id):
            while not release.is_set():
                await asyncio.sleep(0.005)
            return "done"

        monkeypatch.setattr(main, "_run_agent", slow_run)

        accepted = client.post(
            "/invocations", json={"mode": "async", "prompt": "慢任务"}
        ).json()
        assert client.get("/ping").json()["status"] == "HealthyBusy"

        release.set()
        for _ in range(200):
            if client.get("/ping").json()["status"] == "Healthy":
                break
            time.sleep(0.01)
        assert client.get("/ping").json()["status"] == "Healthy"

        final = client.post(
            "/invocations", json={"mode": "status", "taskId": accepted["taskId"]}
        ).json()
        assert final["status"] == "succeeded"


class TestStreamToolDedup:
    """current_tool_use 在工具调用被逐步构建时会反复触发,同一个工具能刷四五次。
    实测流式输出里 [调用工具 business___get_order] 打了 4 遍。
    """

    def test_repeated_tool_events_yield_once(self, runtime, monkeypatch):
        import agent.main as main

        client, _, _ = runtime

        async def fake_stream_events(prompt):
            # 模拟 Strands 的真实行为:同一个工具名反复出现
            for _ in range(4):
                yield {"current_tool_use": {"name": "business___get_order"}}
            yield {"data": "订单"}
            for _ in range(3):
                yield {"current_tool_use": {"name": "business___list_tickets"}}
            yield {"data": "已签收"}

        class FakeAgent:
            def stream_async(self, prompt):
                return fake_stream_events(prompt)

        import contextlib

        @contextlib.asynccontextmanager
        async def fake_session(**kwargs):
            yield FakeAgent()

        monkeypatch.setattr(main, "_persist_turn", lambda *a, **k: None)
        import agent.assembly as assembly

        monkeypatch.setattr(assembly, "agent_session", fake_session)

        with client.stream(
            "POST", "/invocations", json={"mode": "stream", "prompt": "x"}
        ) as response:
            events = [
                json.loads(line[len("data: "):])
                for line in response.iter_lines()
                if line.startswith("data: ")
            ]

        tools = [e["name"] for e in events if e.get("type") == "tool"]
        assert tools == ["business___get_order", "business___list_tickets"], (
            f"工具名应各出现一次,实际 {tools}"
        )


class TestDiagnoseMode:
    """部署后排查用的诊断模式。

    存在的理由是实际踩过的坑:
      * 挂死类故障不产生日志,只能"单独测一步"定位
      * requestHeaderAllowlist 没配时 actor_id 静默变 anonymous,
        而 GetAgentRuntime 不回显这个配置 —— 只能从容器里看
    """

    def test_headers_reports_resolved_actor(self, runtime):
        client, _, _ = runtime
        body = client.post(
            "/invocations", json={"mode": "diagnose", "what": "headers"}
        ).json()

        h = body["headers"]
        assert h["resolved_actor"] == "anonymous"
        assert h["is_anonymous"] is True
        assert h["has_authorization"] is False

    def test_headers_reflects_a_real_jwt(self, runtime):
        import base64

        client, _, _ = runtime
        claims = base64.urlsafe_b64encode(
            json.dumps({"sub": "actor-x", "actor_id": "actor-x",
                        "username": "u", "scope": "tools:read"}).encode()
        ).rstrip(b"=").decode()
        body = client.post(
            "/invocations", json={"mode": "diagnose", "what": "headers"},
            headers={"Authorization": f"Bearer eyJhbGciOiJSUzI1NiJ9.{claims}.sig"},
        ).json()

        h = body["headers"]
        assert h["resolved_actor"] == "actor-x"
        assert h["scopes"] == ["tools:read"]

    def test_never_echoes_the_token(self, runtime):
        """诊断输出可能被贴进工单,绝不能带 token。"""
        import base64

        client, _, _ = runtime
        canary = "CANARY-secret-token-value"
        claims = base64.urlsafe_b64encode(
            json.dumps({"sub": "a", "note": canary}).encode()
        ).rstrip(b"=").decode()
        raw = client.post(
            "/invocations", json={"mode": "diagnose", "what": "headers"},
            headers={"Authorization": f"Bearer eyJ0.{claims}.{canary}"},
        ).text

        assert canary not in raw
        assert "Bearer " in raw  # 只报前缀

    def test_s3_skips_without_a_bucket(self, runtime):
        client, _, _ = runtime
        body = client.post(
            "/invocations", json={"mode": "diagnose", "what": "s3"}
        ).json()
        assert "skipped" in body["s3"] or "ok" in body["s3"]

    def test_unknown_what_does_not_crash(self, runtime):
        client, _, _ = runtime
        body = client.post(
            "/invocations", json={"mode": "diagnose", "what": "nonsense"}
        ).json()
        assert body["what"] == "nonsense"
