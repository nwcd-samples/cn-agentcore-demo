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
        reasoner: True 则用 deepseek-reasoner。注意它是思维链模型,
            不建议在工具循环里用,只用于单轮深度分析。
        stream: 是否流式。Runtime 的 SSE 路径需要 True。
    """
    settings = settings or get_settings()
    model_id = settings.deepseek_reasoner_model if reasoner else settings.deepseek_model

    params: dict[str, Any] = {"max_tokens": settings.max_tokens}
    if not reasoner:
        # reasoner 不接受 temperature,传了会 400
        params["temperature"] = settings.temperature
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
