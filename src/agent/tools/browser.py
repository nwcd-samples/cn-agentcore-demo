"""AgentCore Browser 工具。

用 Playwright over CDP 连 AgentCore 的托管浏览器,去抓自建的物流查询网页。

为什么要用浏览器而不是直接发 HTTP 请求:那个页面刻意做成需要填表提交的形式
(POST /track + 表单字段),模拟真实承运商官网 —— 现实里 Agent 要查物流
也是这样,没有 API 只有网页。

两个必须注意的技术点:

1. **用 async Playwright,不用 sync。**
   Strands 对 async 工具直接 await(sync 工具走 asyncio.to_thread)。
   Agent 跑在事件循环里,sync_playwright() 在事件循环内会直接抛错。
   所以这里全部用 async_playwright。

2. **镜像里没有浏览器内核。**
   connect_over_cdp 连的是远端浏览器,不需要本地 Chromium,
   所以 Dockerfile 里没有 `playwright install`(省几百 MB)。

会话生命周期同 Code Interpreter:惰性启动 + ExitStack 注册清理。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from typing import Any
from urllib.parse import urlparse

from strands import tool

from agent import obs
from agent.config import Settings

LOG = logging.getLogger(__name__)

# 页面元素选择器。必须和 src/lambdas/logistics/logistics_handler.py 里
# render_form() 生成的 id 一致 —— 有测试断言两边不漂移。
SELECTOR_SHIPMENT_INPUT = "#shipment-no"
SELECTOR_QUERY_BUTTON = "#query-btn"

# 运单号格式,和物流 Lambda 的校验保持一致
_SHIPMENT_NO_RE = re.compile(r"^[A-Z]{2,4}[0-9]{8,20}$")

# 抓回来的文本上限。整页纯文本塞给模型既费 token 又容易淹没重点。
_MAX_TEXT_CHARS = 4000

# 单次浏览器操作的超时(毫秒)
_NAV_TIMEOUT_MS = 30_000
# 整个 track_shipment 的上限。Playwright 的 timeout 只管单个动作,
# 挡不住整体挂死。
_TRACK_TIMEOUT_SECONDS = 90
_ACTION_TIMEOUT_MS = 15_000

# live view URL 的有效期上限由 SDK 限定为 300 秒
_LIVE_VIEW_EXPIRES = 300


class BrowserToolError(RuntimeError):
    """浏览器操作失败。"""


def _fix_china_ws_url(ws_url: str, region: str) -> str:
    """修正 SDK 在中国区拼错的 WebSocket 域名。

    【SDK bug,已实测】bedrock_agentcore._utils.endpoints.get_data_plane_endpoint()
    把域名硬编码成:

        f"https://bedrock-agentcore.{region}.amazonaws.com"

    完全没处理 aws-cn 分区。generate_ws_headers() 基于它拼 wss URL,于是在
    cn-northwest-1 得到:

        wss://bedrock-agentcore.cn-northwest-1.amazonaws.com/browser-streams/...
                                                          ↑ 少了 .cn
        -> getaddrinfo ENOTFOUND

    有意思的是同一个文件里的 _validate_endpoint_url() 白名单里【列了】
    ".amazonaws.com.cn" —— 作者知道有这个域,只是拼 URL 时漏了。

    Browser 会话本身是正常启动的(控制面走 boto3,域名由 botocore 解析,
    botocore 的 endpoints.json 是对的),只有这个手工拼的 CDP 地址不对。

    这里只在 cn-* 区且域名确实缺 .cn 时补上,其他分区原样返回。
    SigV4 签名不受影响 —— 签名里的 host 由 SDK 自己算,我们只改连接用的 URL。
    """
    if not region.startswith("cn-"):
        return ws_url
    broken = f"bedrock-agentcore.{region}.amazonaws.com"
    if f"{broken}.cn" in ws_url:
        return ws_url  # SDK 已经修好了
    if broken in ws_url:
        fixed = ws_url.replace(broken, f"{broken}.cn", 1)
        LOG.warning(
            "SDK 在中国区拼错了 WebSocket 域名(缺 .cn),已修正为 %s",
            fixed.split("/browser-streams")[0],
        )
        return fixed
    return ws_url


# ---------------------------------------------------------------------------
# 惰性会话
# ---------------------------------------------------------------------------


class LazyBrowser:
    """按需启动 Browser 会话并连上 Playwright。

    模型不一定用得到浏览器(多数问题查订单就够了),
    所以不在组装阶段就开会话 —— 那会白付冷启动时间和费用。
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: Any = None
        self._playwright: Any = None
        self._browser: Any = None
        self._page: Any = None
        self._lock = asyncio.Lock()

    @property
    def started(self) -> bool:
        return self._client is not None

    def _start_session_blocking(self) -> Any:
        """boto3 调用是阻塞的,放到线程里跑,别卡住事件循环。"""
        from bedrock_agentcore.tools import BrowserClient

        client = BrowserClient(self._settings.region)
        client.start(
            identifier=self._settings.browser_id,
            # 固定视口,保证页面布局稳定,抓取结果可复现
            viewport={"width": 1280, "height": 900},
        )
        ws_url, headers = client.generate_ws_headers()
        # SDK 在 aws-cn 分区把域名拼错了(缺 .cn),这里兜一下
        ws_url = _fix_china_ws_url(ws_url, self._settings.region)
        return client, ws_url, headers

    async def page(self) -> Any:
        """返回一个可用的 Page。首次调用时才真正开会话。"""
        async with self._lock:
            if self._page is not None:
                return self._page

            from playwright.async_api import async_playwright

            LOG.info("启动 Browser 会话,identifier=%s", self._settings.browser_id)
            with obs.span("browser.start", identifier=self._settings.browser_id):
                client, ws_url, headers = await asyncio.to_thread(
                    self._start_session_blocking
                )
            self._client = client

            self._playwright = await async_playwright().start()
            # connect_over_cdp 连远端浏览器,本地不需要 Chromium
            self._browser = await self._playwright.chromium.connect_over_cdp(
                ws_url, headers=headers, timeout=_NAV_TIMEOUT_MS
            )
            # 托管浏览器已经有一个 context 和 page,复用它而不是新建
            contexts = self._browser.contexts
            context = contexts[0] if contexts else await self._browser.new_context()
            pages = context.pages
            self._page = pages[0] if pages else await context.new_page()
            self._page.set_default_timeout(_ACTION_TIMEOUT_MS)
            self._page.set_default_navigation_timeout(_NAV_TIMEOUT_MS)
            LOG.info("Browser 会话已就绪 session=%s", client.session_id)
            return self._page

    def live_view_url(self) -> str | None:
        """拿一个可以在浏览器里实时观看沙箱画面的链接(演示时很有用)。"""
        if self._client is None:
            return None
        try:
            return self._client.generate_live_view_url(expires=_LIVE_VIEW_EXPIRES)
        except Exception:
            LOG.exception("生成 live view URL 失败")
            return None

    async def aclose(self) -> None:
        # 逐层关闭,每层单独 try —— 前面失败不该妨碍后面清理。
        #
        # playwright.stop() 在"连接失败后清理"这条路径上会抛
        #   got Future attached to a different loop
        # 因为它内部的 transport 跑在另一个 loop 上。这不影响会话回收
        # (下面的 client.stop() 才是真正释放沙箱的),所以降级成 debug,
        # 不要用 exception 级别刷栈 —— 否则真正的失败会被这条噪音盖住。
        for label, closer in (
            ("browser", getattr(self._browser, "close", None)),
            ("playwright", getattr(self._playwright, "stop", None)),
        ):
            if closer is None:
                continue
            try:
                await closer()
            except Exception as exc:
                if "different loop" in str(exc):
                    LOG.debug("关闭 %s 时遇到跨事件循环告警(可忽略)", label)
                else:
                    LOG.warning("关闭 %s 失败:%s", label, type(exc).__name__)
        if self._client is not None:
            try:
                await asyncio.to_thread(self._client.stop)
                LOG.info("Browser 会话已关闭")
            except Exception:
                LOG.exception("关闭 Browser 会话失败(会话会自行超时)")
        self._client = self._playwright = self._browser = self._page = None

    def close(self) -> None:
        """给 ExitStack 用的同步收尾。

        清理发生在 agent_session 退出时,那时可能已经不在事件循环里了,
        所以两种情况都要处理。
        """
        if not self.started:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(self.aclose())
            return
        # 在事件循环里:丢到独立线程跑一个新循环,避免 await 一个同步函数
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(asyncio.run, self.aclose()).result(timeout=60)


# ---------------------------------------------------------------------------
# 文本抽取
# ---------------------------------------------------------------------------


def collapse_whitespace(text: str) -> str:
    """把页面文本压成紧凑形式。

    innerText 会带大量连续空行和缩进,原样给模型是纯浪费 token。
    """
    lines = [line.strip() for line in (text or "").splitlines()]
    return "\n".join(line for line in lines if line)


def truncate(text: str, limit: int = _MAX_TEXT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[页面内容过长,已截断,原始 {len(text)} 字符]"


def same_origin(url: str, allowed_base: str) -> bool:
    """只允许访问自建的物流站点。

    不做这个限制的话,模型可以让浏览器去任意 URL —— 那就是一个
    开放代理 / SSRF 面。演示 Browser 能力不需要这种自由度。
    """
    if not allowed_base:
        return False
    target, base = urlparse(url), urlparse(allowed_base)
    if target.scheme not in ("http", "https"):
        return False
    return (target.scheme, target.hostname, target.port) == (
        base.scheme,
        base.hostname,
        base.port,
    )


# ---------------------------------------------------------------------------
# 工具构造
# ---------------------------------------------------------------------------


def build_browser_tools(settings: Settings, stack: contextlib.ExitStack) -> list:
    """构造 Browser 相关工具。

    Args:
        stack: 调用方的 ExitStack,用来注册浏览器会话的清理。
    """
    browser = LazyBrowser(settings)
    stack.callback(browser.close)

    base_url = (settings.logistics_url or "").rstrip("/")

    @tool
    async def track_shipment(shipment_no: str) -> str:
        """在承运商官网上查询运单的物流轨迹。

        这个承运商只有网页没有 API,所以要用浏览器填表查询。
        返回页面上的完整物流轨迹文字,包括异常原因和滞留时长 ——
        订单接口只告诉你"超期了",具体卡在哪一环只有这里能看到。

        Args:
            shipment_no: 运单号,例如 SF7758291046。可以从 get_order 的结果里拿到。
        """
        if not base_url:
            return "没有配置承运商网站地址(LOGISTICS_URL),无法查询物流。"

        cleaned = re.sub(r"\s+", "", shipment_no or "").upper()
        if not _SHIPMENT_NO_RE.match(cleaned):
            return (
                f"运单号格式不对:{shipment_no!r}。"
                "应该是 2-4 位字母加 8-20 位数字,例如 SF7758291046。"
            )

        async def _do_query() -> str:
            page = await browser.page()
            await page.goto(base_url + "/", wait_until="domcontentloaded")
            await page.fill(SELECTOR_SHIPMENT_INPUT, cleaned)
            # 点完要等导航,否则会抓到还没刷新的旧页面
            async with page.expect_navigation(wait_until="domcontentloaded"):
                await page.click(SELECTOR_QUERY_BUTTON)
            return await page.inner_text("body")

        try:
            with obs.span("browser.track_shipment") as sp:
                # 整步加超时上限:Playwright 自己的 timeout 只管单个动作,
                # 挡不住"连上了但一直没响应"。任何一步都不该无限期挂着 ——
                # 那会让调用方一路读超时,看起来像服务挂了。
                text = await asyncio.wait_for(
                    _do_query(), timeout=_TRACK_TIMEOUT_SECONDS
                )
                sp["page_chars"] = len(text or "")
        except asyncio.TimeoutError:
            LOG.warning("浏览器查询运单超时 shipment_no=%s", cleaned)
            return (
                f"物流网站查询超时(超过 {_TRACK_TIMEOUT_SECONDS} 秒)。"
                "可以告知用户稍后重试,或改用订单里的物流状态。"
            )
        except Exception as exc:  # noqa: BLE001
            LOG.exception("浏览器查询运单失败 shipment_no=%s", cleaned)
            return (
                f"物流网站查询失败({type(exc).__name__})。"
                "可以告知用户稍后重试,或改用订单里的物流状态。"
            )

        content = truncate(collapse_whitespace(text))
        if "查询无结果" in content or "没有查询到" in content:
            return f"承运商网站上查不到运单 {cleaned}。请确认运单号是否正确。"
        LOG.info("已抓取运单 %s 的物流页面(%d 字符)", cleaned, len(content))
        return f"承运商网站上运单 {cleaned} 的页面内容:\n\n{content}"

    @tool
    async def browse_page(url: str) -> str:
        """打开指定网页并返回正文文字。

        只允许访问承运商网站,其他地址会被拒绝。
        一般用不到它 —— 查物流直接用 track_shipment 更省事。

        Args:
            url: 完整 URL,必须属于承运商网站。
        """
        if not base_url:
            return "没有配置允许访问的网站(LOGISTICS_URL)。"
        if not same_origin(url, base_url):
            return (
                f"拒绝访问 {url}。只允许访问承运商网站 {base_url},"
                "这是刻意的限制,不要尝试其他地址。"
            )
        try:
            page = await browser.page()
            await page.goto(url, wait_until="domcontentloaded")
            text = await page.inner_text("body")
        except Exception as exc:  # noqa: BLE001
            LOG.exception("打开页面失败 url=%s", url)
            return f"打开页面失败({type(exc).__name__})。"
        return truncate(collapse_whitespace(text))

    @tool
    async def browser_live_view() -> str:
        """拿一个链接,可以在浏览器里实时观看沙箱正在做什么。

        只在用户明确要求"给我看看过程"时使用。链接 5 分钟内有效。
        """
        if not browser.started:
            return "浏览器会话还没启动,先做一次实际的网页查询再来取。"
        url = await asyncio.to_thread(browser.live_view_url)
        if not url:
            return "生成实时画面链接失败。"
        return f"实时画面(5 分钟内有效):\n{url}"

    return [track_shipment, browse_page, browser_live_view]


__all__ = [
    "build_browser_tools",
    "LazyBrowser",
    "BrowserToolError",
    "collapse_whitespace",
    "truncate",
    "same_origin",
    "SELECTOR_SHIPMENT_INPUT",
    "SELECTOR_QUERY_BUTTON",
]
