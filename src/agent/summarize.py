"""会话摘要。

MemoryLite 的对话窗口是固定的(默认 12 轮)。超出窗口的历史如果直接丢掉,
长会话会突然"失忆" —— 用户前面说过的运单号、已经确认过的结论全没了。

这里把窗口之外的历史压成一段摘要,由 assembly 拼进 system prompt。
摘要用的是不带工具的单轮调用,便宜且不会触发副作用。
"""

from __future__ import annotations

import logging

from agent.config import Settings, get_settings
from agent.memory_lite import MAX_SUMMARY_CHARS, MemoryLite, Turn

LOG = logging.getLogger(__name__)

SUMMARY_PROMPT = """\
把下面这段客服对话压缩成要点,供后续对话参考。

必须保留:订单号、运单号、工单号、金额、已经向用户承诺过的事、
用户明确表达的偏好和诉求。
不要保留:寒暄、重复确认、工具调用的原始输出。

用中文,不超过 300 字,直接给要点,不要写"以下是摘要"之类的开场。

对话内容:
{conversation}
"""

# 喂给摘要模型的原文上限。超长就只取最近的部分 ——
# 更早的内容已经被上一次摘要覆盖过了。
_MAX_CONVERSATION_CHARS = 12_000


def render_conversation(turns: list[Turn]) -> str:
    lines = [
        f"{'用户' if t.role == 'user' else '助手'}:{t.content}" for t in turns
    ]
    text = "\n".join(lines)
    if len(text) > _MAX_CONVERSATION_CHARS:
        text = "...(更早的内容已在上一版摘要中)\n" + text[-_MAX_CONVERSATION_CHARS:]
    return text


def summarize_turns(turns: list[Turn], settings: Settings | None = None) -> str:
    """调模型生成摘要。失败返回空串 —— 摘要失败不该影响正常对话。"""
    if not turns:
        return ""
    settings = settings or get_settings()

    try:
        from strands import Agent

        from agent.model import build_model

        # 不给工具、不流式:纯文本压缩,避免任何副作用
        agent = Agent(
            model=build_model(settings, stream=False),
            system_prompt="你是一个会话摘要助手,只输出要点,不调用任何工具。",
            tools=[],
            callback_handler=None,
        )
        result = agent(SUMMARY_PROMPT.format(conversation=render_conversation(turns)))
        return str(result).strip()[:MAX_SUMMARY_CHARS]
    except Exception:
        LOG.exception("生成会话摘要失败,本轮跳过")
        return ""


def maybe_summarize(
    memory: MemoryLite,
    session_id: str,
    *,
    settings: Settings | None = None,
    summarizer=summarize_turns,
) -> bool:
    """需要时生成并保存摘要。返回是否真的写了新摘要。

    summarizer 可注入,方便测试时不去打真实模型。
    """
    if not session_id or not memory.needs_summary(session_id):
        return False

    older = memory.messages_outside_window(session_id)
    if not older:
        return False

    existing, _ = memory.get_summary(session_id)
    turns = older
    if existing:
        # 把上一版摘要作为第一条"用户消息"喂进去,实现滚动摘要。
        # created_at 用最早那条消息的时间,保证它排在最前面。
        turns = [
            Turn(
                role="user",
                content=f"[已有摘要] {existing}",
                created_at=older[0].created_at - 1,
            )
        ] + older

    summary = summarizer(turns, settings)
    if not summary:
        return False

    total = memory.count_messages(session_id)
    window = memory._settings.stm_max_turns * 2
    memory.set_summary(session_id, summary, covered_upto=max(0, total - window))
    LOG.info("已更新会话 %s 的摘要(覆盖前 %d 条)", session_id, max(0, total - window))
    return True


__all__ = ["maybe_summarize", "summarize_turns", "render_conversation", "SUMMARY_PROMPT"]
