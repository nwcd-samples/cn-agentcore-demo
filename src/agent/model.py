"""接 DeepSeek 官方 API。

中国区 Bedrock 不提供基座模型,所以模型走 DeepSeek 官方的 OpenAI 兼容端点。
Strands 的 OpenAIModel 直接能用,把 base_url 指过去就行。

API Key 的取法有两条路:
  * 线上:AgentCore Identity 的 API Key credential provider 现取,
    镜像和 Runtime 配置里都没有明文。
  * 本地:读 DEEPSEEK_API_KEY 环境变量,方便调试。

模型分工:
  * deepseek-chat     跑工具循环(function calling 稳定)
  * deepseek-reasoner 只用于无工具的深度分析步骤
"""

from __future__ import annotations

import logging
from typing import Any

from strands.models.openai import OpenAIModel

from agent.config import Settings, get_settings

LOG = logging.getLogger(__name__)

# 进程内缓存,避免每次调用都去 Identity 换一次 key
_cached_api_key: str | None = None


class _DropReasoningContentWarning(logging.Filter):
    """过滤掉 Strands 关于 reasoningContent 的那一条重复告警。

    类定义在模块级而不是函数内 —— 放函数里每次调用都是一个新的类对象,
    幂等检查用的 isinstance 永远为假,过滤器会一次次叠加上去。
    """

    TARGET = "reasoningContent is not supported in multi-turn conversations"

    def filter(self, record: logging.LogRecord) -> bool:
        return self.TARGET not in record.getMessage()


def _quiet_reasoning_content_warning() -> None:
    """压掉 Strands 关于 reasoningContent 的重复告警。

    2026-09 起 DeepSeek 所有模型都返回 reasoning_content。而工具循环本质是
    多轮对话,Strands 每轮都会把上一轮的助手消息回传,于是每轮都撞上这条:

        reasoningContent is not supported in multi-turn conversations
        with the Chat Completions API.

    读过 strands.models.openai 的源码确认:它只是 logger.warning 然后把
    reasoningContent 从 content 里过滤掉,**不会失败**。所以这条告警对我们
    没有信息量 —— 但一次多工具对话能刷十几条,把真正的错误淹掉,
    在 CloudWatch 里尤其糟。

    只装一个精确匹配这条消息的过滤器,不动 Strands 的其他日志。
    """
    logger = logging.getLogger("strands.models.openai")
    if not any(isinstance(f, _DropReasoningContentWarning) for f in logger.filters):
        logger.addFilter(_DropReasoningContentWarning())


_quiet_reasoning_content_warning()


def _fetch_api_key_from_identity(provider_name: str) -> str:
    """从 AgentCore Identity 的 token vault 取 DeepSeek API Key。

    刻意装饰一个**同步**函数:SDK 的 sync_wrapper 会自己判断当前有没有事件循环,
    并用 contextvars.copy_context() 把上下文带进工作线程。

    这一点很关键 —— workload access token 是存在 ContextVar 里的
    (BedrockAgentCoreContext),自己起线程跑 asyncio.run 会丢掉它,
    结果就是线上取不到凭证而本地却好使。别自己造这个轮子。
    """
    from bedrock_agentcore.identity.auth import requires_api_key

    @requires_api_key(provider_name=provider_name)
    def _grab(*, api_key: str) -> str:
        return api_key

    return _grab()


def get_api_key(settings: Settings | None = None, *, refresh: bool = False) -> str:
    global _cached_api_key
    settings = settings or get_settings()

    if _cached_api_key and not refresh:
        return _cached_api_key

    if settings.deepseek_api_key_env:
        LOG.info("DeepSeek API key 来自环境变量(本地调试模式)")
        _cached_api_key = settings.deepseek_api_key_env
        return _cached_api_key

    LOG.info("从 AgentCore Identity 取 DeepSeek API key,provider=%s",
             settings.deepseek_api_key_provider)
    _cached_api_key = _fetch_api_key_from_identity(settings.deepseek_api_key_provider)
    if not _cached_api_key:
        raise RuntimeError(
            f"Identity provider {settings.deepseek_api_key_provider} 没返回 API key,"
            "先跑 scripts/setup_identity.py"
        )
    return _cached_api_key


def build_model(
    settings: Settings | None = None,
    *,
    reasoner: bool = False,
    stream: bool = True,
    extra_params: dict[str, Any] | None = None,
) -> OpenAIModel:
    """构造指向 DeepSeek 的 Strands 模型。

    Args:
        reasoner: True 则用 DEEPSEEK_REASONER_MODEL(默认 deepseek-v4-pro)。
            用于需要更强推理的单轮分析。
        stream: 是否流式。Runtime 的 SSE 路径需要 True。

    2026-09 实测的 DeepSeek 现状(和早期文档不一样,改之前先重测):
      * /models 只列出 deepseek-flash 和 deepseek-v4-pro;
        deepseek-chat / deepseek-reasoner 仍可用但只是别名,都路由到 flash
      * 【所有】模型都返回 reasoning_content —— 不再存在"普通模型 vs 思维链模型"
        的区分,所以 reasoner 开关只是选一个更强的模型,不是换一类模型
      * 三个模型都接受 temperature。早期 deepseek-reasoner 拒绝该参数,
        现在不会了,所以不再做特殊分支
      * flash 和 v4-pro 都支持 function calling(已用带 business___ 前缀的
        工具名实测,能正确抽出参数)
    """
    settings = settings or get_settings()
    model_id = settings.deepseek_reasoner_model if reasoner else settings.deepseek_model

    params: dict[str, Any] = {
        # 所有模型都带思维链,reasoning token 也算进 max_tokens。
        # 给太小会出现"content 为空但 finish_reason=stop"—— 思维链把额度吃光了。
        # 实测 max_tokens=32 就会踩到。
        "max_tokens": settings.max_tokens,
        "temperature": settings.temperature,
    }
    if extra_params:
        params.update(extra_params)

    return OpenAIModel(
        client_args={
            "api_key": get_api_key(settings),
            "base_url": settings.deepseek_base_url,
            # DeepSeek 偶发 5xx,交给 SDK 自己退避重试
            "max_retries": 3,
            "timeout": 120.0,
        },
        model_id=model_id,
        stream=stream,
        params=params,
    )
