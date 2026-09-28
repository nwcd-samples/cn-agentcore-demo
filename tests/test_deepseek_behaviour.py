"""针对 DeepSeek 实际行为的测试(2026-09 实测基线)。

这些断言锁住的是**真实 API 行为**,不是文档描述。DeepSeek 的模型阵容在项目
进行中变过一次,所以这里既记录当前状态,也防止有人按过时的假设改回去。

实测结论(真实 key 打过):
  * /models 只列出 deepseek-flash 和 deepseek-v4-pro;
    deepseek-chat / deepseek-reasoner 仍可用,但只是指向 flash 的别名
  * 所有模型都返回 reasoning_content —— 不存在"普通 vs 思维链"两类模型
  * 三个模型都接受 temperature(早期 deepseek-reasoner 会拒绝)
  * flash 和 v4-pro 都支持 function calling,能正确解析带 business___
    前缀的工具名并抽出参数
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def clean_model_env(monkeypatch):
    """清掉可能影响默认值的环境变量。"""
    from agent.config import get_settings

    for key in ("DEEPSEEK_MODEL", "DEEPSEEK_REASONER_MODEL"):
        monkeypatch.delenv(key, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# 模型 ID
# ---------------------------------------------------------------------------


class TestModelIds:
    def test_defaults_are_real_ids_not_aliases(self):
        """默认值必须是 /models 真列出来的 ID。

        deepseek-chat / deepseek-reasoner 现在只是别名,依赖别名意味着
        DeepSeek 哪天撤掉它就全线失效。
        """
        from agent.config import get_settings

        settings = get_settings()
        assert settings.deepseek_model == "deepseek-flash"
        assert settings.deepseek_reasoner_model == "deepseek-v4-pro"

    def test_no_alias_names_in_defaults(self):
        from agent.config import get_settings

        settings = get_settings()
        aliases = {"deepseek-chat", "deepseek-reasoner"}
        assert settings.deepseek_model not in aliases
        assert settings.deepseek_reasoner_model not in aliases

    def test_env_can_still_override(self, monkeypatch):
        from agent.config import get_settings

        monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-chat")
        get_settings.cache_clear()
        assert get_settings().deepseek_model == "deepseek-chat"

    def test_base_url_is_official_endpoint(self):
        from agent.config import get_settings

        assert get_settings().deepseek_base_url == "https://api.deepseek.com/v1"


# ---------------------------------------------------------------------------
# temperature
# ---------------------------------------------------------------------------


class TestTemperature:
    def _params(self, reasoner: bool) -> dict:
        """构造模型并取出它传给 SDK 的 params。"""
        from agent.config import get_settings
        from agent.model import build_model

        model = build_model(get_settings(), reasoner=reasoner, stream=False)
        return model.get_config().get("params") or {}

    def test_temperature_is_sent_for_both_models(self):
        """早期 deepseek-reasoner 拒绝 temperature,所以代码里曾有个
        "reasoner 就不传"的分支。实测三个模型现在都接受,分支已删除。
        """
        assert "temperature" in self._params(reasoner=False)
        assert "temperature" in self._params(reasoner=True)

    def test_no_reasoner_special_case_left_in_source(self):
        """静态兜底:源码里不该再有针对 reasoner 的 temperature 分支。"""
        source = (REPO_ROOT / "src" / "agent" / "model.py").read_text()
        assert "if not reasoner:" not in source

    def test_max_tokens_is_generous_enough_for_reasoning(self):
        """所有模型都带思维链,reasoning token 也算进 max_tokens。
        给太小会出现 content 为空但 finish_reason=stop —— 实测 32 就会踩到。
        """
        params = self._params(reasoner=False)
        assert params["max_tokens"] >= 1024, (
            "max_tokens 太小,思维链会把额度吃光,content 返回空"
        )


# ---------------------------------------------------------------------------
# reasoningContent 告警
# ---------------------------------------------------------------------------


class TestReasoningContentWarningFilter:
    """工具循环是多轮对话,Strands 每轮都会回传上一轮的助手消息,
    其中含 reasoningContent。Strands 只是 warning + 过滤,不会失败,
    但一次多工具对话能刷十几条,把真正的错误淹掉。
    """

    TARGET = (
        "reasoningContent is not supported in multi-turn conversations "
        "with the Chat Completions API."
    )

    def test_target_warning_is_suppressed(self, caplog):
        import agent.model  # noqa: F401  导入即安装过滤器

        logger = logging.getLogger("strands.models.openai")
        with caplog.at_level(logging.WARNING, logger="strands.models.openai"):
            logger.warning(self.TARGET)
        assert self.TARGET not in caplog.text

    def test_other_warnings_from_the_same_logger_still_pass(self, caplog):
        """只压这一条,不能把整个 logger 静音 ——
        那样会把真正的模型层问题也藏掉。
        """
        import agent.model  # noqa: F401

        logger = logging.getLogger("strands.models.openai")
        with caplog.at_level(logging.WARNING, logger="strands.models.openai"):
            logger.warning("rate limited, retrying")
        assert "rate limited, retrying" in caplog.text

    def test_filter_is_installed_only_once(self):
        """模块可能被重复 import,过滤器不该叠加。"""
        import agent.model as model_mod

        logger = logging.getLogger("strands.models.openai")
        before = len(logger.filters)
        model_mod._quiet_reasoning_content_warning()
        model_mod._quiet_reasoning_content_warning()
        assert len(logger.filters) == before

    def test_filter_matches_on_substring_not_exact(self):
        """Strands 可能给这条消息加前后缀,用子串匹配更稳。"""
        import agent.model  # noqa: F401

        logger = logging.getLogger("strands.models.openai")
        drop = [f for f in logger.filters if type(f).__name__ == "_DropReasoningContentWarning"]
        assert drop, "过滤器没装上"
        record = logging.LogRecord(
            "strands.models.openai", logging.WARNING, __file__, 1,
            "note: " + self.TARGET + " (see docs)", None, None,
        )
        assert drop[0].filter(record) is False


# ---------------------------------------------------------------------------
# 文档与代码一致
# ---------------------------------------------------------------------------


class TestDocumentedBehaviour:
    def test_model_py_records_the_measurement_date(self):
        """这些结论是某个时点的实测值,必须标注日期 ——
        否则下次 DeepSeek 又改了,没人知道该重测。
        """
        source = (REPO_ROOT / "src" / "agent" / "model.py").read_text()
        assert "2026-09" in source
        assert "实测" in source

    def test_env_example_explains_the_alias_situation(self):
        """.env.example 是使用者第一眼看到的地方,别名这件事要写在那里。"""
        text = (REPO_ROOT / ".env.example").read_text()
        assert "deepseek-flash" in text
        assert "别名" in text

    def test_config_default_matches_env_example(self):
        """.env.example 里的值必须和 config.py 的默认值一致,
        否则使用者复制了 example 反而得到不同行为。"""
        from agent.config import get_settings

        text = (REPO_ROOT / ".env.example").read_text()
        settings = get_settings()
        assert f"DEEPSEEK_MODEL={settings.deepseek_model}" in text
        assert f"DEEPSEEK_REASONER_MODEL={settings.deepseek_reasoner_model}" in text
