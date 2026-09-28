"""AgentCore Runtime 的容器入口。

用 bedrock_agentcore 的 BedrockAgentCoreApp 而不是自己写 FastAPI:
它已经提供了 Runtime 契约要求的全部东西 ——
  POST /invocations   entrypoint,返回生成器就自动变成 SSE
  GET  /ping          健康检查,@app.async_task 期间自动上报 HealthyBusy
  会话/请求上下文     session_id 与请求头从 RequestContext 拿

覆盖的 Runtime 能力(对应 README 的能力矩阵):
  基础调用   mode=sync
  流式       mode=stream          -> 返回异步生成器,框架转 SSE
  异步长任务 mode=async           -> 后台跑,/ping 变 HealthyBusy
  会话隔离   session_id 透传给 MemoryLite
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from typing import Any

from bedrock_agentcore.runtime import BedrockAgentCoreApp, RequestContext

from agent import obs
from agent.config import get_settings
from agent.identity import caller_from_headers
from agent.memory_lite import MemoryLite

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
LOG = logging.getLogger("agent")

app = BedrockAgentCoreApp()

# 后台任务的结果暂存。生产上该放 DynamoDB;demo 里进程内够用,
# 因为 Runtime 的会话亲和性保证同一 sessionId 落在同一个 MicroVM。
_async_results: dict[str, dict[str, Any]] = {}


def _extract_prompt(payload: dict[str, Any]) -> str:
    """兼容几种常见的入参写法,省得调用方记格式。"""
    for key in ("prompt", "input", "message", "query", "text"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _resolve_session(context: RequestContext | None, payload: dict[str, Any]) -> str:
    # Runtime 透传的 sessionId 优先;本地调试时允许从 payload 给
    if context and context.session_id:
        return context.session_id
    return str(payload.get("sessionId") or payload.get("session_id") or uuid.uuid4())


async def _run_agent(prompt: str, *, session_id: str, actor_id: str) -> str:
    """跑一轮完整对话,返回最终文本,并把这一轮写进 STM。"""
    from agent.assembly import agent_session

    settings = get_settings()
    memory = MemoryLite(settings)

    # with 覆盖整轮:Gateway 的 MCP 会话和沙箱会话都要活到工具调用结束
    async with agent_session(
        session_id=session_id, actor_id=actor_id, settings=settings,
        memory=memory, stream=False,
    ) as agent:
        result = await agent.invoke_async(prompt)
        answer = str(result)

    _persist_turn(memory, session_id, prompt, answer)
    return answer


async def _diagnose(
    payload: dict[str, Any], context: RequestContext | None, caller: Any, settings: Any
) -> dict[str, Any]:
    """按需返回诊断信息。用 {"mode":"diagnose","what":"..."} 调。

    what 取值:
      headers  容器实际收到了哪些请求头,以及解出的 actor_id
               —— 验证 requestHeaderAllowlist 是否生效。GetAgentRuntime
                  不回显那个配置,这是唯一可靠的检查方式。
      s3       单独测一次产物桶写入,分别报客户端创建和 put 的耗时
      step     只跑某一个自检步骤:{"component":"Browser"}
      all      以上全部(默认)
    """
    import time as _t

    what = str(payload.get("what") or "all").lower()
    out: dict[str, Any] = {"what": what}

    if what in ("headers", "all"):
        hdrs = (context.request_headers if context else None) or {}
        auth = next((v for k, v in hdrs.items() if k.lower() == "authorization"), "")
        out["headers"] = {
            "names": sorted(hdrs),
            "has_authorization": bool(auth),
            # 只报长度和前缀,不回显 token
            "auth_prefix": auth[:7],
            "auth_len": len(auth),
            "resolved_actor": caller.actor_id,
            "is_anonymous": caller.is_anonymous,
            "username": caller.username,
            "scopes": list(caller.scopes),
        }

    if what in ("s3", "all"):
        res: dict[str, Any] = {"bucket": settings.artifact_bucket}
        if not settings.artifact_bucket:
            res["skipped"] = "ARTIFACT_BUCKET 未配置"
        else:
            t0 = _t.monotonic()
            try:
                from agent.selftest import _get_s3_client

                s3 = _get_s3_client(settings)
                res["client_ms"] = int((_t.monotonic() - t0) * 1000)
                t1 = _t.monotonic()
                s3.put_object(
                    Bucket=settings.artifact_bucket,
                    Key=f"outputs/diagnose/{int(_t.time())}.txt",
                    Body=b"diagnose", ServerSideEncryption="AES256",
                )
                res["put_ms"] = int((_t.monotonic() - t1) * 1000)
                res["ok"] = True
            except Exception as exc:  # noqa: BLE001
                res["ok"] = False
                res["error"] = f"{type(exc).__name__}: {exc}"[:200]
        out["s3"] = res

    if what == "step":
        from agent.selftest import Report, _run_step_async, build_steps

        want = str(payload.get("component") or "")
        report = Report(
            session_id="diagnose", actor_id=caller.actor_id,
            region=settings.region, project=settings.project,
            started_at=int(_t.time()),
        )
        steps = [
            s for s in build_steps(settings, "diagnose", caller.actor_id)
            if not want or s[1] == want
        ]
        t0 = _t.monotonic()
        for name, component, fn in steps:
            await _run_step_async(report, name, component, fn)
        out["elapsed_ms"] = int((_t.monotonic() - t0) * 1000)
        out["steps"] = [s.to_dict() for s in report.steps]

    return out


def _persist_turn(memory: MemoryLite, session_id: str, prompt: str, answer: str) -> None:
    """落盘这一轮,并在超出窗口时滚动更新摘要。

    摘要是在【写完历史之后】做的,所以下一轮拼 system prompt 时就能用上。
    摘要失败只记日志 —— 它是增强项,不该拖垮对话。
    """
    memory.append_turn(session_id, "user", prompt)
    memory.append_turn(session_id, "assistant", answer)
    try:
        from agent.summarize import maybe_summarize

        maybe_summarize(memory, session_id, settings=get_settings())
    except Exception:
        LOG.exception("会话摘要更新失败(不影响本轮对话)")


@app.entrypoint
async def invoke(payload: dict[str, Any], context: RequestContext):
    settings = get_settings()
    session_id = _resolve_session(context, payload)
    caller = caller_from_headers(context.request_headers if context else None)
    mode = str(payload.get("mode") or "sync").lower()

    LOG.info(
        "invocation mode=%s session=%s actor=%s anonymous=%s",
        mode, session_id, caller.actor_id, caller.is_anonymous,
    )
    # 把会话/用户打到当前 span 上,CloudWatch GenAI 看板才能按它们筛。
    # Strands 会在 agent span 上再设一遍,但 selftest / status 这些
    # 不经过 Strands 的路径只能靠这里。
    obs.set_session_attributes(session_id, caller.actor_id, settings.project)

    # ---- 诊断模式 ----
    #
    # 部署后排查用。这几项都是实际踩过坑的地方,单独探测比翻日志快:
    #   * 挂死类故障不产生日志,只能靠"单独测一步"定位
    #   * requestHeaderAllowlist 没配时 actor_id 静默变 anonymous,
    #     而 GetAgentRuntime 不回显这个配置,只能从容器里看
    if mode == "diagnose":
        return await _diagnose(payload, context, caller, settings)

    # ---- 任务状态查询    # ---- 任务状态查询:异步模式的配套接口 ----
    if mode == "status":
        task_id = str(payload.get("taskId") or "")
        record = _async_results.get(task_id)
        if record is None:
            return {"taskId": task_id, "status": "unknown"}
        return {"taskId": task_id, **record}

    # ---- 自检:依次点亮六个中国区可用组件 ----
    if mode == "selftest":
        from agent.selftest import run_selftest

        return await run_selftest(
            settings, session_id=session_id, actor_id=caller.actor_id
        )

    prompt = _extract_prompt(payload)
    if not prompt and mode != "status":
        return {"error": "缺少 prompt 字段", "hint": "{\"prompt\": \"...\"}"}

    # ---- 流式:返回异步生成器,框架会转成 SSE ----
    if mode == "stream":
        return _stream_agent(prompt, session_id=session_id, actor_id=caller.actor_id)

    # ---- 异步长任务:立刻返回 taskId,后台继续跑 ----
    if mode == "async":
        task_id = str(uuid.uuid4())
        _async_results[task_id] = {"status": "running", "startedAt": int(time.time())}
        # 不能 await,否则就不是异步了;交给事件循环后台跑
        asyncio.create_task(
            _run_async_task(task_id, prompt, session_id=session_id, actor_id=caller.actor_id)
        )
        return {
            "taskId": task_id,
            "status": "running",
            "hint": '查询进度:{"mode":"status","taskId":"%s"}' % task_id,
        }

    # ---- 同步 ----
    answer = await _run_agent(prompt, session_id=session_id, actor_id=caller.actor_id)
    response: dict[str, Any] = {
        "sessionId": session_id,
        "actorId": caller.actor_id,
        "answer": answer,
    }
    # 带上 trace id:线上出问题时可以直接拿它去 CloudWatch 查这一次调用
    trace_id = obs.current_trace_id()
    if trace_id:
        response["traceId"] = trace_id
    return response


async def _stream_agent(prompt: str, *, session_id: str, actor_id: str):
    """逐 token 产出。

    产出的每个 dict 都会被框架 JSON 序列化成一个 SSE data 帧。
    """
    from agent.assembly import agent_session

    settings = get_settings()
    memory = MemoryLite(settings)

    chunks: list[str] = []
    # current_tool_use 会在工具调用被逐步构建的过程中【反复】触发,
    # 同一个工具能刷四五次。只在工具名变化时才产出一帧。
    last_tool = ""
    try:
        # with 必须包住整个 stream_async 迭代,否则工具会话会提前关闭
        async with agent_session(
            session_id=session_id, actor_id=actor_id, settings=settings,
            memory=memory, stream=True,
        ) as agent:
            async for event in agent.stream_async(prompt):
                # 文本增量
                if "data" in event:
                    chunk = event["data"]
                    chunks.append(chunk)
                    yield {"type": "text", "delta": chunk}
                # 工具调用开始,便于前端显示"正在查订单…"
                elif "current_tool_use" in event:
                    tool_use = event["current_tool_use"] or {}
                    name = tool_use.get("name") or ""
                    if name and name != last_tool:
                        last_tool = name
                        yield {"type": "tool", "name": name}
    except Exception as exc:  # noqa: BLE001
        LOG.exception("流式执行失败")
        yield {"type": "error", "message": str(exc)}
        return

    answer = "".join(chunks)
    _persist_turn(memory, session_id, prompt, answer)
    yield {"type": "done", "sessionId": session_id}


@app.async_task
async def _run_async_task(task_id: str, prompt: str, *, session_id: str, actor_id: str) -> None:
    """被 @app.async_task 装饰,期间 /ping 会返回 HealthyBusy。

    这就是 Runtime "异步长任务" 能力的演示点:Runtime 看到 HealthyBusy
    就知道这个实例还在干活,不会把它回收掉。
    """
    started = _async_results[task_id]["startedAt"]
    try:
        answer = await _run_agent(prompt, session_id=session_id, actor_id=actor_id)
        _async_results[task_id] = {
            "status": "succeeded",
            "startedAt": started,
            "finishedAt": int(time.time()),
            "answer": answer,
        }
    except Exception as exc:  # noqa: BLE001
        LOG.exception("异步任务 %s 失败", task_id)
        _async_results[task_id] = {
            "status": "failed",
            "startedAt": started,
            "finishedAt": int(time.time()),
            "error": str(exc),
        }


if __name__ == "__main__":
    # Runtime 要求监听 8080
    app.run(port=int(os.environ.get("PORT", "8080")))
