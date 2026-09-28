"""Browser 工具的测试。

用假 Playwright + 假 BrowserClient。重点覆盖:

1. **SSRF 防护**。模型能指定 URL,不加限制就等于把托管浏览器变成开放代理。
2. **会话生命周期**。惰性启动,用完必须关(含异常路径)。
3. **async 工具契约**。Agent 跑在事件循环里,sync_playwright 会直接抛错,
   所以工具必须是 async 的 —— 有静态断言兜底。
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
from pathlib import Path

import pytest


@pytest.fixture
def browser_mod():
    import agent.tools.browser as module

    return module


# ---------------------------------------------------------------------------
# 假 Playwright / 假 BrowserClient
# ---------------------------------------------------------------------------


# Python 3.11+ 起内置 TimeoutError 就是 asyncio.TimeoutError,
# 用它当"动作失败"会被 track_shipment 的超时分支截获,测不到想测的路径。
# 这里用 Playwright 真实的异常类型。
from playwright.async_api import Error as PlaywrightError


class FakePage:
    def __init__(self, pages_html: dict[str, str]):
        self._html = pages_html
        self.url = ""
        self.filled: dict[str, str] = {}
        self.clicked: list[str] = []
        self.default_timeout = None
        self.default_nav_timeout = None
        self.goto_calls: list[str] = []
        self.fail_on: str | None = None

    def set_default_timeout(self, ms):
        self.default_timeout = ms

    def set_default_navigation_timeout(self, ms):
        self.default_nav_timeout = ms

    async def goto(self, url, wait_until=None):
        if self.fail_on == "goto":
            raise PlaywrightError("net::ERR_CONNECTION_REFUSED")
        self.goto_calls.append(url)
        self.url = url

    async def fill(self, selector, value):
        if self.fail_on == "fill":
            raise PlaywrightError(f"locator not found: {selector}")
        self.filled[selector] = value

    async def click(self, selector):
        self.clicked.append(selector)
        # 点击查询按钮后页面变成结果页
        shipment = self.filled.get("#shipment-no", "")
        self.url = "/track"
        self._current = self._html.get(shipment, self._html.get("__notfound__", ""))

    def expect_navigation(self, wait_until=None):
        @contextlib.asynccontextmanager
        async def _ctx():
            yield None

        return _ctx()

    async def inner_text(self, selector):
        if self.fail_on == "inner_text":
            raise PlaywrightError("body not found")
        return getattr(self, "_current", self._html.get("__index__", ""))


class FakeContext:
    def __init__(self, page):
        self.pages = [page]

    async def new_page(self):
        return self.pages[0]


class FakeBrowser:
    def __init__(self, page):
        self.contexts = [FakeContext(page)]
        self.closed = False

    async def close(self):
        self.closed = True


class FakeChromium:
    def __init__(self, page):
        self._page = page
        self.connect_calls: list[dict] = []

    async def connect_over_cdp(self, url, headers=None, timeout=None):
        self.connect_calls.append({"url": url, "headers": headers, "timeout": timeout})
        return FakeBrowser(self._page)


class FakePlaywrightHandle:
    def __init__(self, page):
        self.chromium = FakeChromium(page)
        self.stopped = False

    async def stop(self):
        self.stopped = True


class FakePlaywrightFactory:
    """模拟 async_playwright() 返回的对象(需要 .start())。"""

    def __init__(self, page):
        self._page = page
        self.handle: FakePlaywrightHandle | None = None

    def __call__(self):
        return self

    async def start(self):
        self.handle = FakePlaywrightHandle(self._page)
        return self.handle


class FakeBrowserClient:
    instances: list["FakeBrowserClient"] = []

    def __init__(self, region):
        self.region = region
        self.session_id = "browser-sess-1"
        self.started_with = None
        self.viewport = None
        self.stopped = False
        self.live_view_calls: list[int] = []
        FakeBrowserClient.instances.append(self)

    def start(self, identifier=None, viewport=None, **kwargs):
        self.started_with = identifier
        self.viewport = viewport
        return self.session_id

    def generate_ws_headers(self):
        return (
            "wss://bedrock-agentcore.cn-northwest-1.amazonaws.com.cn"
            "/browser-streams/aws.browser.v1/sessions/browser-sess-1/automation",
            {"Authorization": "AWS4-HMAC-SHA256 ...", "Host": "x"},
        )

    def generate_live_view_url(self, expires=300):
        self.live_view_calls.append(expires)
        return f"https://live.example/view?X-Amz-Expires={expires}"

    def stop(self):
        self.stopped = True
        return True


LOGISTICS_BASE = "https://abc123.execute-api.cn-northwest-1.amazonaws.com.cn"

TRACK_PAGE = """宁夏速运
运单 SF7758291046
异常
包裹已在中转环节停留 72 小时,疑似中转异常,建议联系客服处理
承运商 顺丰
关联订单 ORD-1024
当前位置 陕西西安中转中心
物流轨迹
2026-09-25 14:00
因分拣设备故障,快件滞留待处理
陕西西安中转中心
"""

NOT_FOUND_PAGE = "查询无结果\n没有查询到运单 SF0000000001 的信息。"


@pytest.fixture
def fake_browser(monkeypatch, browser_mod):
    """把 BrowserClient 和 async_playwright 都换掉。"""
    import bedrock_agentcore.tools as tools_pkg
    import playwright.async_api as pw

    FakeBrowserClient.instances.clear()
    page = FakePage(
        {
            "__index__": "运单查询\n请输入运单号",
            "SF7758291046": TRACK_PAGE,
            "__notfound__": NOT_FOUND_PAGE,
        }
    )
    factory = FakePlaywrightFactory(page)

    monkeypatch.setattr(tools_pkg, "BrowserClient", FakeBrowserClient)
    monkeypatch.setattr(pw, "async_playwright", factory)
    return {"page": page, "factory": factory, "client_cls": FakeBrowserClient}


@pytest.fixture
def tools(browser_mod, fake_browser):
    from agent.config import get_settings

    settings = get_settings()
    object.__setattr__(settings, "logistics_url", LOGISTICS_BASE)
    with contextlib.ExitStack() as stack:
        built = browser_mod.build_browser_tools(settings, stack)
        yield {t.tool_name: t for t in built}
    object.__setattr__(settings, "logistics_url", "")


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# 纯函数
# ---------------------------------------------------------------------------


class TestTextHelpers:
    def test_collapse_whitespace_drops_blank_and_indent(self, browser_mod):
        raw = "  运单查询  \n\n\n      顺丰   \n\n  ORD-1024\n\n"
        assert browser_mod.collapse_whitespace(raw) == "运单查询\n顺丰\nORD-1024"

    def test_collapse_handles_empty(self, browser_mod):
        assert browser_mod.collapse_whitespace("") == ""
        assert browser_mod.collapse_whitespace(None) == ""

    def test_truncate_marks_the_cut(self, browser_mod):
        text = "x" * 10_000
        out = browser_mod.truncate(text)
        assert len(out) < len(text)
        assert "已截断" in out and "10000" in out

    def test_short_text_is_untouched(self, browser_mod):
        assert browser_mod.truncate("短文本") == "短文本"


class TestSameOrigin:
    @pytest.mark.parametrize(
        "url",
        [
            LOGISTICS_BASE,
            LOGISTICS_BASE + "/",
            LOGISTICS_BASE + "/track",
            LOGISTICS_BASE + "/health?x=1",
        ],
    )
    def test_allows_the_configured_site(self, browser_mod, url):
        assert browser_mod.same_origin(url, LOGISTICS_BASE) is True

    @pytest.mark.parametrize(
        "url",
        [
            "https://evil.example.com/",
            "http://abc123.execute-api.cn-northwest-1.amazonaws.com.cn/",  # 降级到 http
            "https://abc123.execute-api.cn-northwest-1.amazonaws.com.cn.evil.com/",
            "https://169.254.169.254/latest/meta-data/",  # 实例元数据
            "file:///etc/passwd",
            "http://localhost:8080/",
            "https://abc123.execute-api.cn-northwest-1.amazonaws.com.cn:8443/",
            "javascript:alert(1)",
            "",
        ],
    )
    def test_rejects_everything_else(self, browser_mod, url):
        """不加这个限制,托管浏览器就是一个开放代理 / SSRF 面。"""
        assert browser_mod.same_origin(url, LOGISTICS_BASE) is False

    def test_rejects_when_no_base_configured(self, browser_mod):
        assert browser_mod.same_origin("https://anything/", "") is False


# ---------------------------------------------------------------------------
# 惰性会话
# ---------------------------------------------------------------------------


class TestLazyBrowser:
    def test_does_not_start_until_first_page(self, browser_mod, fake_browser):
        from agent.config import get_settings

        lazy = browser_mod.LazyBrowser(get_settings())
        assert lazy.started is False
        assert FakeBrowserClient.instances == []

        run(lazy.page())
        assert lazy.started is True
        assert len(FakeBrowserClient.instances) == 1

    def test_reuses_the_same_page(self, browser_mod, fake_browser):
        from agent.config import get_settings

        lazy = browser_mod.LazyBrowser(get_settings())

        async def twice():
            return await lazy.page(), await lazy.page()

        first, second = run(twice())
        assert first is second
        assert len(FakeBrowserClient.instances) == 1

    def test_passes_identifier_and_fixed_viewport(self, browser_mod, fake_browser):
        """固定视口让页面布局稳定,抓取结果可复现。"""
        from agent.config import get_settings

        lazy = browser_mod.LazyBrowser(get_settings())
        run(lazy.page())
        client = FakeBrowserClient.instances[0]
        assert client.started_with == "aws.browser.v1"
        assert client.viewport == {"width": 1280, "height": 900}

    def test_connects_with_sigv4_headers(self, browser_mod, fake_browser):
        """CDP 连接必须带 generate_ws_headers 给的 SigV4 头,否则会被拒。"""
        from agent.config import get_settings

        lazy = browser_mod.LazyBrowser(get_settings())
        run(lazy.page())
        call = fake_browser["factory"].handle.chromium.connect_calls[0]
        assert call["url"].startswith("wss://")
        assert "Authorization" in call["headers"]

    def test_aclose_tears_down_every_layer(self, browser_mod, fake_browser):
        from agent.config import get_settings

        lazy = browser_mod.LazyBrowser(get_settings())

        async def flow():
            await lazy.page()
            await lazy.aclose()

        run(flow())
        assert FakeBrowserClient.instances[0].stopped is True
        assert fake_browser["factory"].handle.stopped is True
        assert lazy.started is False

    def test_close_is_noop_when_never_started(self, browser_mod, fake_browser):
        from agent.config import get_settings

        browser_mod.LazyBrowser(get_settings()).close()
        assert FakeBrowserClient.instances == []

    def test_close_works_from_inside_an_event_loop(self, browser_mod, fake_browser):
        """ExitStack 的清理可能发生在事件循环里(agent_session 退出时),
        也可能不在。两种都要能收尾。"""
        from agent.config import get_settings

        lazy = browser_mod.LazyBrowser(get_settings())

        async def flow():
            await lazy.page()
            lazy.close()  # 同步调用,但此刻在事件循环里

        run(flow())
        assert FakeBrowserClient.instances[0].stopped is True

    def test_close_survives_a_failing_layer(self, browser_mod, fake_browser):
        from agent.config import get_settings

        lazy = browser_mod.LazyBrowser(get_settings())

        async def flow():
            page = await lazy.page()
            del page
            lazy._browser.close = lambda: (_ for _ in ()).throw(RuntimeError("断连"))
            await lazy.aclose()

        run(flow())
        # 浏览器关不掉,但会话还是得停
        assert FakeBrowserClient.instances[0].stopped is True


class TestSessionLifecycleViaExitStack:
    def test_closed_on_stack_exit(self, browser_mod, fake_browser):
        from agent.config import get_settings

        settings = get_settings()
        object.__setattr__(settings, "logistics_url", LOGISTICS_BASE)
        with contextlib.ExitStack() as stack:
            built = {t.tool_name: t for t in browser_mod.build_browser_tools(settings, stack)}
            run(built["track_shipment"](shipment_no="SF7758291046"))
            assert FakeBrowserClient.instances[0].stopped is False
        assert FakeBrowserClient.instances[0].stopped is True
        object.__setattr__(settings, "logistics_url", "")

    def test_closed_even_if_body_raises(self, browser_mod, fake_browser):
        from agent.config import get_settings

        settings = get_settings()
        object.__setattr__(settings, "logistics_url", LOGISTICS_BASE)
        with pytest.raises(ValueError):
            with contextlib.ExitStack() as stack:
                built = {
                    t.tool_name: t
                    for t in browser_mod.build_browser_tools(settings, stack)
                }
                run(built["track_shipment"](shipment_no="SF7758291046"))
                raise ValueError("对话中途失败")
        assert FakeBrowserClient.instances[0].stopped is True
        object.__setattr__(settings, "logistics_url", "")

    def test_unused_browser_never_starts(self, browser_mod, fake_browser):
        from agent.config import get_settings

        with contextlib.ExitStack() as stack:
            browser_mod.build_browser_tools(get_settings(), stack)
        assert FakeBrowserClient.instances == []


# ---------------------------------------------------------------------------
# track_shipment
# ---------------------------------------------------------------------------


class TestTrackShipment:
    def test_fills_the_form_and_clicks(self, tools, fake_browser):
        """必须真的填表 + 点击 —— 页面只接受 POST,GET 拿不到结果。"""
        result = str(run(tools["track_shipment"](shipment_no="SF7758291046")))

        page = fake_browser["page"]
        assert page.goto_calls == [LOGISTICS_BASE + "/"]
        assert page.filled["#shipment-no"] == "SF7758291046"
        assert page.clicked == ["#query-btn"]
        assert "停留 72 小时" in result
        assert "分拣设备故障" in result

    def test_normalizes_the_shipment_no(self, tools, fake_browser):
        run(tools["track_shipment"](shipment_no="  sf7758291046 "))
        assert fake_browser["page"].filled["#shipment-no"] == "SF7758291046"

    @pytest.mark.parametrize(
        "bad", ["", "123", "SF-123", "notashipment", "SF123", "   "]
    )
    def test_malformed_input_is_rejected_before_opening_the_browser(
        self, tools, fake_browser, bad
    ):
        result = str(run(tools["track_shipment"](shipment_no=bad)))
        assert "格式不对" in result
        assert FakeBrowserClient.instances == []

    def test_not_found_page_is_recognized(self, tools, fake_browser):
        result = str(run(tools["track_shipment"](shipment_no="SF0000000001")))
        assert "查不到运单" in result

    def test_navigation_failure_is_reported_not_raised(self, tools, fake_browser):
        """工具抛异常会中断 Agent 循环,必须返回文本。"""
        fake_browser["page"].fail_on = "goto"
        result = str(run(tools["track_shipment"](shipment_no="SF7758291046")))
        assert "查询失败" in result
        assert "Error" in result

    def test_missing_selector_failure_is_reported(self, tools, fake_browser):
        fake_browser["page"].fail_on = "fill"
        result = str(run(tools["track_shipment"](shipment_no="SF7758291046")))
        assert "查询失败" in result

    def test_without_configured_site_it_says_so(self, browser_mod, fake_browser):
        from agent.config import get_settings

        settings = get_settings()
        object.__setattr__(settings, "logistics_url", "")
        with contextlib.ExitStack() as stack:
            built = {t.tool_name: t for t in browser_mod.build_browser_tools(settings, stack)}
            result = str(run(built["track_shipment"](shipment_no="SF7758291046")))
        assert "LOGISTICS_URL" in result

    def test_long_page_is_truncated(self, tools, fake_browser):
        fake_browser["page"]._html["SF7758291046"] = "行\n" * 20_000
        result = str(run(tools["track_shipment"](shipment_no="SF7758291046")))
        assert "已截断" in result


# ---------------------------------------------------------------------------
# browse_page
# ---------------------------------------------------------------------------


class TestBrowsePage:
    def test_allows_the_configured_site(self, tools, fake_browser):
        result = str(run(tools["browse_page"](url=LOGISTICS_BASE + "/")))
        assert "运单查询" in result

    @pytest.mark.parametrize(
        "url",
        [
            "https://evil.example.com/steal",
            "http://169.254.169.254/latest/meta-data/iam/security-credentials/",
            "file:///etc/passwd",
            "http://localhost:9000/admin",
        ],
    )
    def test_refuses_other_origins(self, tools, fake_browser, url):
        result = str(run(tools["browse_page"](url=url)))
        assert "拒绝访问" in result
        # 关键:连会话都不该开
        assert FakeBrowserClient.instances == []

    def test_refusal_message_discourages_retrying(self, tools, fake_browser):
        """错误信息要明确告诉模型这是刻意限制,否则它会换着 URL 反复试。"""
        result = str(run(tools["browse_page"](url="https://evil.example.com/")))
        assert "刻意的限制" in result


# ---------------------------------------------------------------------------
# live view
# ---------------------------------------------------------------------------


class TestLiveView:
    def test_requires_an_active_session(self, tools, fake_browser):
        result = str(run(tools["browser_live_view"]()))
        assert "还没启动" in result

    def test_returns_a_presigned_url(self, tools, fake_browser):
        run(tools["track_shipment"](shipment_no="SF7758291046"))
        result = str(run(tools["browser_live_view"]()))
        assert "https://live.example/view" in result
        # SDK 限定最大 300 秒
        assert FakeBrowserClient.instances[0].live_view_calls == [300]

    def test_expiry_stays_within_the_sdk_limit(self, browser_mod):
        assert browser_mod._LIVE_VIEW_EXPIRES <= 300


# ---------------------------------------------------------------------------
# 契约
# ---------------------------------------------------------------------------


class TestToolContract:
    def test_all_browser_tools_are_async(self, browser_mod, fake_browser):
        """Agent 跑在事件循环里,sync_playwright() 在循环内会直接抛错。
        Strands 对 async 工具直接 await,所以这些工具必须是 coroutine。"""
        from agent.config import get_settings

        with contextlib.ExitStack() as stack:
            built = browser_mod.build_browser_tools(get_settings(), stack)
        for t in built:
            assert inspect.iscoroutinefunction(t._tool_func), (
                f"{t.tool_name} 不是 async,会在事件循环里炸"
            )

    def test_module_does_not_use_sync_playwright(self):
        """只检查实际代码行 —— 文档注释里会提到 sync_playwright 说明为什么不用它。"""
        import ast
        from pathlib import Path

        path = (
            Path(__file__).resolve().parents[1]
            / "src" / "agent" / "tools" / "browser.py"
        )
        source = path.read_text()
        tree = ast.parse(source)

        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
                for alias in node.names:
                    imported.add(f"{node.module}.{alias.name}")
            elif isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)

        assert "playwright.sync_api" not in imported
        assert any(name.startswith("playwright.async_api") for name in imported), (
            f"没有 import async_playwright,实际 import 了:{sorted(imported)}"
        )

        # AST 层面确认没有 sync_playwright(...) 调用。
        # 不用文本匹配 —— 模块的 docstring 里刻意提到了这个名字来说明为什么不用它。
        called = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert "sync_playwright" not in called
        assert "async_playwright" in called

    def test_tool_names_are_stable(self, tools):
        assert set(tools) == {"track_shipment", "browse_page", "browser_live_view"}

    def test_system_prompt_mentions_track_shipment(self):
        from agent.assembly import SYSTEM_PROMPT

        assert "track_shipment" in SYSTEM_PROMPT

    def test_dockerfile_does_not_install_browser_binaries(self):
        """connect_over_cdp 连远端浏览器,本地不需要 Chromium。
        装了会让镜像白白大几百 MB。"""
        from pathlib import Path

        dockerfile = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text()
        assert "playwright install" not in dockerfile


# ---------------------------------------------------------------------------
# 中国区 WebSocket 域名修正
# ---------------------------------------------------------------------------


class TestChinaWebSocketUrlFix:
    """bedrock_agentcore 的 get_data_plane_endpoint() 把域名硬编码成
    f"https://bedrock-agentcore.{region}.amazonaws.com",没处理 aws-cn 分区。
    generate_ws_headers() 基于它拼 wss URL,在 cn-northwest-1 得到的地址
    缺 .cn 后缀,实测 getaddrinfo ENOTFOUND。

    Browser 会话本身能正常启动(控制面走 botocore,域名是对的),
    只有这个手工拼的 CDP 地址不对。
    """

    PATH = "/browser-streams/aws.browser.v1/sessions/01ABC/automation"

    def test_adds_missing_cn_suffix(self, browser_mod):
        broken = f"wss://bedrock-agentcore.cn-northwest-1.amazonaws.com{self.PATH}"
        fixed = browser_mod._fix_china_ws_url(broken, "cn-northwest-1")
        assert fixed == (
            f"wss://bedrock-agentcore.cn-northwest-1.amazonaws.com.cn{self.PATH}"
        )

    def test_is_idempotent(self, browser_mod):
        """SDK 哪天修好了,我们不能再补一次变成 .cn.cn。"""
        good = f"wss://bedrock-agentcore.cn-northwest-1.amazonaws.com.cn{self.PATH}"
        assert browser_mod._fix_china_ws_url(good, "cn-northwest-1") == good

    def test_beijing_region_too(self, browser_mod):
        broken = f"wss://bedrock-agentcore.cn-north-1.amazonaws.com{self.PATH}"
        fixed = browser_mod._fix_china_ws_url(broken, "cn-north-1")
        assert fixed.startswith("wss://bedrock-agentcore.cn-north-1.amazonaws.com.cn/")

    @pytest.mark.parametrize("region", ["us-west-2", "eu-central-1", "ap-northeast-1"])
    def test_global_regions_untouched(self, browser_mod, region):
        """全球区的地址本来就是对的,绝不能动。"""
        url = f"wss://bedrock-agentcore.{region}.amazonaws.com{self.PATH}"
        assert browser_mod._fix_china_ws_url(url, region) == url

    def test_unrecognized_host_is_left_alone(self, browser_mod):
        """endpoint override 之类的自定义域名不该被改。"""
        url = f"wss://my-custom-endpoint.example.com{self.PATH}"
        assert browser_mod._fix_china_ws_url(url, "cn-northwest-1") == url

    def test_fix_is_applied_on_the_real_path(self, browser_mod, fake_browser):
        """确认 _start_session_blocking 真的调用了修正函数 ——
        光有函数没接上等于没修。"""
        from agent.config import get_settings

        settings = get_settings()
        object.__setattr__(settings, "region", "cn-northwest-1")

        class BrokenUrlClient(FakeBrowserClient):
            def generate_ws_headers(self):
                return (
                    "wss://bedrock-agentcore.cn-northwest-1.amazonaws.com"
                    "/browser-streams/aws.browser.v1/sessions/X/automation",
                    {"Authorization": "AWS4-HMAC-SHA256 ..."},
                )

        import bedrock_agentcore.tools as tools_pkg

        original = tools_pkg.BrowserClient
        tools_pkg.BrowserClient = BrokenUrlClient
        try:
            lazy = browser_mod.LazyBrowser(settings)
            _client, ws_url, _headers = lazy._start_session_blocking()
        finally:
            tools_pkg.BrowserClient = original

        assert ".amazonaws.com.cn/" in ws_url, "修正没接到真实调用路径上"


class TestTrackShipmentTimeout:
    """整步超时。Playwright 自己的 timeout 只管单个动作,挡不住
    "连上了但一直没响应" —— 实测踩过:OTEL 的 logs exporter 在中国区
    DNS 解析失败后不断重试,把请求线程拖死,Browser 这步就无限期挂着,
    调用方一路读超时,看起来像服务挂了。
    """

    def test_hanging_query_is_cut_off(self, browser_mod, fake_browser, monkeypatch):
        from agent.config import get_settings

        settings = get_settings()
        object.__setattr__(settings, "logistics_url", LOGISTICS_BASE)
        monkeypatch.setattr(browser_mod, "_TRACK_TIMEOUT_SECONDS", 0.3)

        async def hang(self, url, wait_until=None):
            await asyncio.sleep(30)

        monkeypatch.setattr(FakePage, "goto", hang)

        with contextlib.ExitStack() as stack:
            tools = {
                t.tool_name: t for t in browser_mod.build_browser_tools(settings, stack)
            }
            result = str(run(tools["track_shipment"](shipment_no="SF7758291046")))

        assert "超时" in result
        object.__setattr__(settings, "logistics_url", "")

    def test_timeout_message_tells_the_model_what_to_do(self, browser_mod):
        """超时提示要给出替代方案,否则模型会反复重试同一个调用。"""
        source = (
            Path(__file__).resolve().parents[1]
            / "src" / "agent" / "tools" / "browser.py"
        ).read_text()
        assert "改用订单里的物流状态" in source

    def test_timeout_is_bounded_and_reasonable(self, browser_mod):
        assert 30 <= browser_mod._TRACK_TIMEOUT_SECONDS <= 180


class TestCleanupNeverCrossesEventLoops:
    """Playwright 对象绑定在创建时的事件循环上。从别的循环 await 它们会
    直接挂死 —— 不抛异常、不打日志、不超时。

    这是 selftest 静默超时的根因,排查时因为完全没有日志,连着六轮定位错了
    地方。早先的写法是"在循环里就开个新线程跑 asyncio.run(aclose())",
    看着像是规避了"循环里不能 run"的限制,实际制造了跨循环 await。
    """

    def test_sync_close_does_not_spawn_a_loop(self, browser_mod):
        """close() 里不许出现 asyncio.run / new_event_loop /
        ThreadPoolExecutor —— 那都是跨循环 await 的前兆。"""
        import ast

        source = (
            Path(__file__).resolve().parents[1]
            / "src" / "agent" / "tools" / "browser.py"
        ).read_text()
        tree = ast.parse(source)

        close_fn = next(
            n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "close"
        )
        calls = {ast.unparse(n.func) for n in ast.walk(close_fn)
                 if isinstance(n, ast.Call)}
        for forbidden in ("asyncio.run", "asyncio.new_event_loop",
                          "concurrent.futures.ThreadPoolExecutor"):
            assert forbidden not in calls, (
                f"close() 用了 {forbidden},会跨事件循环 await Playwright 并挂死"
            )

    def test_sync_close_still_releases_the_paid_session(
        self, browser_mod, fake_browser
    ):
        """跳过 Playwright 清理是可以的(transport 随进程回收),
        但 AgentCore 的浏览器会话必须停 —— 那个占沙箱、要计费。"""
        from agent.config import get_settings

        lazy = browser_mod.LazyBrowser(get_settings())
        run(lazy.page())
        lazy.close()

        assert FakeBrowserClient.instances[0].stopped is True
        assert lazy.started is False

    def test_async_stack_gets_full_cleanup(self, browser_mod, fake_browser):
        """AsyncExitStack 能注册异步清理,这时应该走 aclose() 做完整关闭。"""
        from agent.config import get_settings

        settings = get_settings()
        object.__setattr__(settings, "logistics_url", LOGISTICS_BASE)

        async def flow():
            async with contextlib.AsyncExitStack() as stack:
                tools = {
                    t.tool_name: t
                    for t in browser_mod.build_browser_tools(settings, stack)
                }
                await tools["track_shipment"](shipment_no="SF7758291046")

        run(flow())
        # 完整清理:会话停掉,playwright 也 stop 了
        assert FakeBrowserClient.instances[0].stopped is True
        assert fake_browser["factory"].handle.stopped is True
        object.__setattr__(settings, "logistics_url", "")

    def test_async_callback_is_preferred(self, browser_mod, fake_browser):
        """有 push_async_callback 就该用它,而不是退化成同步 close。"""
        from agent.config import get_settings

        registered: list[str] = []

        class SpyStack(contextlib.AsyncExitStack):
            def push_async_callback(self, cb, *a, **k):
                registered.append(cb.__name__)
                return super().push_async_callback(cb, *a, **k)

            def callback(self, cb, *a, **k):
                registered.append(cb.__name__)
                return super().callback(cb, *a, **k)

        async def flow():
            async with SpyStack() as stack:
                browser_mod.build_browser_tools(get_settings(), stack)

        run(flow())
        assert registered == ["aclose"], f"注册的是 {registered},应该是 aclose"
