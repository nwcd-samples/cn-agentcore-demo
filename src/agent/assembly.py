"""组装 Strands Agent。

用上下文管理器而不是简单的工厂函数,原因是 Gateway 的 MCP 会话有生命周期:
Strands 的 MCPClient 拿到的工具只在会话存活期间可调用,拿完 tools 就关掉
client 的话,工具调用会在运行时失败。所以这里用 ExitStack 把所有需要清理的
资源串起来,调用方用 `with agent_session(...) as agent:` 保证覆盖整轮对话。

工具按能力分文件放在 agent/tools/ 下,这里只负责拼装,
并且**按可用性降级** —— 某个能力还没部署(比如 Gateway 还没建),
Agent 依然能起来,只是少几个工具。demo 的部署是分阶段的,
不能因为 P2 没做就让 P0 起不来。
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterator

from strands import Agent

from agent.config import Settings, get_settings
from agent.memory_lite import MemoryLite
from agent.model import build_model

LOG = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
你是「宁夏云上售后助手」,运行在 Amazon Bedrock AgentCore(cn-northwest-1)上。

职责:处理订单查询、物流异常、超时赔付计算和工单创建。

工作方式:
- 需要事实时一定调工具,不要凭印象编造订单号、金额或物流状态。
- 处理具体订单问题时,先用 business___get_order 查订单。
- 订单显示超期或有运单号时,用 track_shipment 去承运商网站查真实轨迹 ——
  订单接口只说"超期了",卡在哪一环、为什么卡,只有物流页面上写着。
- 算赔付金额分两步:先用 business___get_refund_policy 拿规则,
  再用 run_python 在沙箱里按分(cents)计算。不要心算,也不要用浮点累加。
- 要出图表时:用 run_python 里的 matplotlib 存成 PNG,
  再用 publish_file 拿到下载链接给用户。
- 开工单前先用 business___list_tickets 看是否已有同类工单,避免重复开单。
  开单时把物流页面上看到的具体异常原因写进 summary,不要只写"超期"。
- 用户提到的偏好(通知方式、联系时段等)用 remember 记下来。
- 工具报错时说明失败原因和用户可以怎么做,不要假装成功。

金额一律以分为单位存储和计算,展示给用户时再换算成元。
回答用中文,简洁,先给结论再给依据。
"""


def _collect_tools(
    settings: Settings,
    memory: MemoryLite,
    *,
    actor_id: str,
    session_id: str,
    stack: contextlib.ExitStack,
) -> list:
    """按可用性收集工具。任何一项加载失败都只记日志,不影响其他工具。

    ImportError 和其他异常分开记:前者是"这个能力还没实现/没部署"
    (demo 分阶段交付,P1 阶段就没有 code_interp 和 browser),属于预期情况;
    后者是真的坏了,要带堆栈。混在一起记会让日志失去信噪比。
    """
    tools: list = []

    def _try(label: str, loader) -> None:
        try:
            tools.extend(loader())
        except ImportError:
            LOG.info("%s 尚未实现或未安装,跳过", label)
        except Exception:
            LOG.exception("%s 加载失败", label)

    # MemoryLite 的读写工具:始终可用,只依赖 DynamoDB。
    # actor_id 用闭包绑死,不暴露成工具参数,避免模型越权读别人数据。
    def _memory_tools():
        from agent.tools.memory_tools import build_memory_tools_for

        return build_memory_tools_for(memory, actor_id)

    _try("memory 工具", _memory_tools)

    # Code Interpreter:沙箱执行,中国区可用
    def _code_tools():
        from agent.tools.code_interp import build_code_tools

        return build_code_tools(
            settings, stack, session_id=session_id, actor_id=actor_id
        )

    _try("code interpreter 工具", _code_tools)

    # Browser:沙箱浏览器,中国区可用
    def _browser_tools():
        from agent.tools.browser import build_browser_tools

        return build_browser_tools(settings, stack)

    _try("browser 工具", _browser_tools)

    # Gateway 的 MCP 工具:要等 Gateway 建好并配好 GATEWAY_URL
    if settings.gateway_url:
        def _gateway_tools():
            from agent.tools.gateway import load_gateway_tools

            client, gateway_tools = load_gateway_tools(settings)
            # 会话必须活到本轮对话结束,否则工具调用会失败
            stack.callback(client.stop, None, None, None)
            return gateway_tools

        _try("gateway 工具", _gateway_tools)
    else:
        LOG.warning("GATEWAY_URL 未设置,本轮没有业务工具可用")

    LOG.info("已装载 %d 个工具", len(tools))
    return tools


def _build_system_prompt(memory: MemoryLite, actor_id: str, session_id: str) -> str:
    """把会话摘要和长期记忆拼进 system prompt。

    摘要放前面:它是"这轮对话前面发生了什么",比长期偏好更紧要。
    任何一步失败都降级到基础 prompt —— 读记忆失败不该让对话挂掉。
    """
    prompt = SYSTEM_PROMPT

    try:
        summary, _ = memory.get_summary(session_id)
    except Exception:
        LOG.exception("读会话摘要失败,本轮不带摘要")
        summary = ""
    if summary:
        prompt += (
            "\n\n本次会话较早部分的要点(超出对话窗口,已压缩):\n"
            f"{summary}\n"
        )

    try:
        facts = memory.get_facts(actor_id)
    except Exception:
        LOG.exception("读长期记忆失败,本轮不带用户偏好")
        return prompt
    if facts:
        rendered = "\n".join(f"- {k}: {v}" for k, v in sorted(facts.items()))
        prompt += f"\n\n已知的用户长期偏好(来自历史会话):\n{rendered}\n"
    return prompt


@contextlib.contextmanager
def agent_session(
    *,
    session_id: str,
    actor_id: str,
    settings: Settings | None = None,
    memory: MemoryLite | None = None,
    stream: bool = True,
) -> Iterator[Agent]:
    """构造一个 Agent,并在退出时清理所有下游会话(MCP / 沙箱)。"""
    settings = settings or get_settings()
    memory = memory or MemoryLite(settings)

    with contextlib.ExitStack() as stack:
        # 会话隔离:history 只从当前 session_id 读
        history = memory.as_strands_messages(session_id)

        yield Agent(
            model=build_model(settings, stream=stream),
            system_prompt=_build_system_prompt(memory, actor_id, session_id),
            messages=history,
            tools=_collect_tools(
                settings, memory, actor_id=actor_id, session_id=session_id, stack=stack
            ),
            # 进 Observability 的 trace 属性,方便在看板上按会话/用户筛
            trace_attributes={
                "session.id": session_id,
                "user.id": actor_id,
                "project": settings.project,
            },
            # 历史窗口已由 MemoryLite 控制,不需要 Strands 再裁剪一遍
            callback_handler=None,
        )
