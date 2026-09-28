"""集中管理配置。

原则:能从 AgentCore Runtime 注入的环境变量就读环境变量,
密钥一律不放环境变量 —— DeepSeek 的 API Key 走 AgentCore Identity 的
credential provider 现取(见 model.py),这样镜像和任务定义里都没有明文。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    project: str
    region: str

    # ---- 模型 ----
    # DeepSeek 官方 API,OpenAI 兼容
    deepseek_base_url: str
    deepseek_model: str
    deepseek_reasoner_model: str
    # Identity 里存 DeepSeek API Key 的 credential provider 名
    deepseek_api_key_provider: str
    # 只在本地调试时用;线上留空,走 Identity
    deepseek_api_key_env: str = field(repr=False, default="")

    # ---- 数据 ----
    business_table: str = ""
    memory_table: str = ""
    artifact_bucket: str = ""

    # ---- 下游能力 ----
    gateway_url: str = ""
    gateway_oauth_provider: str = ""
    code_interpreter_id: str = "aws.codeinterpreter.v1"
    browser_id: str = "aws.browser.v1"
    logistics_url: str = ""

    # ---- 行为 ----
    max_tokens: int = 2048
    temperature: float = 0.2
    # 短期记忆保留的对话轮数
    stm_max_turns: int = 12
    stm_ttl_seconds: int = 24 * 3600

    @property
    def is_local(self) -> bool:
        """本地跑(没有 Identity 可用)时为 True。"""
        return bool(self.deepseek_api_key_env)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    project = _env("PROJECT", "agentcore-cn")
    return Settings(
        project=project,
        region=_env("AWS_REGION", "cn-northwest-1"),
        deepseek_base_url=_env("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
        # 2026-09 实测:/models 只列出 deepseek-flash 和 deepseek-v4-pro。
        # deepseek-chat / deepseek-reasoner 仍能用但只是别名,
        # 默认值用真实 ID,不依赖别名今后是否保留。
        deepseek_model=_env("DEEPSEEK_MODEL", "deepseek-flash"),
        deepseek_reasoner_model=_env("DEEPSEEK_REASONER_MODEL", "deepseek-v4-pro"),
        deepseek_api_key_provider=_env("DEEPSEEK_API_KEY_PROVIDER", f"{project}-deepseek"),
        deepseek_api_key_env=_env("DEEPSEEK_API_KEY"),
        business_table=_env("BUSINESS_TABLE", f"{project}-business"),
        memory_table=_env("MEMORY_TABLE", f"{project}-memory"),
        artifact_bucket=_env("ARTIFACT_BUCKET"),
        gateway_url=_env("GATEWAY_URL"),
        gateway_oauth_provider=_env("GATEWAY_OAUTH_PROVIDER", f"{project}-gateway-oauth"),
        code_interpreter_id=_env("CODE_INTERPRETER_ID", "aws.codeinterpreter.v1"),
        browser_id=_env("BROWSER_ID", "aws.browser.v1"),
        logistics_url=_env("LOGISTICS_URL"),
        max_tokens=_env_int("MAX_TOKENS", 2048),
        stm_max_turns=_env_int("STM_MAX_TURNS", 12),
        stm_ttl_seconds=_env_int("STM_TTL_SECONDS", 24 * 3600),
    )
