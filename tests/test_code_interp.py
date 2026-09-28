"""Code Interpreter 工具的测试。

重点覆盖三块容易出错的地方:

1. 事件流解析。InvokeCodeInterpreter 把**异常也放在流里**
   (accessDeniedException 等字段),不是抛出来的。漏检就会把一次失败
   当成"空输出"静默咽掉,模型会以为代码跑成功了。

2. 惰性会话。模型不用沙箱就不该开会话(冷启动 + 计费),
   用完必须关 —— 包括异常路径。

3. 路径与包白名单。模型写的 sandbox_path 和包名都是不可信输入。
"""

from __future__ import annotations

import contextlib

import pytest


@pytest.fixture
def code_interp():
    import agent.tools.code_interp as module

    return module


# ---------------------------------------------------------------------------
# 构造响应事件流
# ---------------------------------------------------------------------------


def stream_response(*events) -> dict:
    return {"sessionId": "sess-1", "stream": list(events)}


def ok_event(
    stdout: str = "",
    stderr: str = "",
    exit_code: int = 0,
    execution_time: float | None = None,
    texts: list[str] | None = None,
    resources: list[dict] | None = None,
    is_error: bool = False,
) -> dict:
    structured: dict = {"stdout": stdout, "stderr": stderr, "exitCode": exit_code}
    if execution_time is not None:
        structured["executionTime"] = execution_time
    content: list[dict] = [{"type": "text", "text": t} for t in (texts or [])]
    for res in resources or []:
        content.append(res)
    return {"result": {"structuredContent": structured, "content": content,
                       "isError": is_error}}


# ---------------------------------------------------------------------------
# 事件流解析
# ---------------------------------------------------------------------------


class TestParseInvokeResult:
    def test_extracts_stdout_stderr_and_exit_code(self, code_interp):
        parsed = code_interp.parse_invoke_result(
            stream_response(ok_event(stdout="194.85\n", stderr="warn\n", exit_code=0))
        )
        assert parsed["stdout"] == "194.85\n"
        assert parsed["stderr"] == "warn\n"
        assert parsed["exit_code"] == 0
        assert parsed["is_error"] is False

    def test_concatenates_output_across_events(self, code_interp):
        """一次执行的输出可能分成多个事件回来。"""
        parsed = code_interp.parse_invoke_result(
            stream_response(
                ok_event(stdout="第一段\n"),
                ok_event(stdout="第二段\n", exit_code=0),
            )
        )
        assert parsed["stdout"] == "第一段\n第二段\n"

    def test_execution_time_converted_to_milliseconds(self, code_interp):
        """API 给的是秒(double),摊平成毫秒整数更好读。"""
        parsed = code_interp.parse_invoke_result(
            stream_response(ok_event(execution_time=1.234))
        )
        assert parsed["execution_time_ms"] == 1234

    def test_collects_text_blocks(self, code_interp):
        parsed = code_interp.parse_invoke_result(
            stream_response(ok_event(texts=["表格渲染结果", "第二块"]))
        )
        assert parsed["texts"] == ["表格渲染结果", "第二块"]

    def test_collects_generated_resources(self, code_interp):
        parsed = code_interp.parse_invoke_result(
            stream_response(
                ok_event(
                    resources=[
                        {"type": "resource_link", "name": "chart.png",
                         "uri": "file:///chart.png", "mimeType": "image/png"}
                    ]
                )
            )
        )
        assert parsed["resources"] == [
            {"type": "resource_link", "name": "chart.png",
             "uri": "file:///chart.png", "mime_type": "image/png"}
        ]

    def test_is_error_flag_is_surfaced(self, code_interp):
        parsed = code_interp.parse_invoke_result(
            stream_response(ok_event(stderr="Traceback...", exit_code=1, is_error=True))
        )
        assert parsed["is_error"] is True
        assert parsed["exit_code"] == 1

    @pytest.mark.parametrize(
        "key",
        [
            "accessDeniedException",
            "throttlingException",
            "validationException",
            "resourceNotFoundException",
            "serviceQuotaExceededException",
            "conflictException",
            "internalServerException",
        ],
    )
    def test_exceptions_in_the_stream_are_raised(self, code_interp, key):
        """这是最容易漏的一条:异常是流里的一个字段,不是抛出来的。
        不显式检查就会把失败当成空输出。"""
        with pytest.raises(code_interp.SandboxError, match=key):
            code_interp.parse_invoke_result(
                stream_response({key: {"message": "配额用完了"}})
            )

    def test_exception_message_is_included(self, code_interp):
        with pytest.raises(code_interp.SandboxError, match="配额用完了"):
            code_interp.parse_invoke_result(
                stream_response({"throttlingException": {"message": "配额用完了"}})
            )

    def test_missing_message_does_not_crash(self, code_interp):
        with pytest.raises(code_interp.SandboxError, match="没有给出原因"):
            code_interp.parse_invoke_result(
                stream_response({"accessDeniedException": {}})
            )

    def test_empty_stream_is_tolerated(self, code_interp):
        parsed = code_interp.parse_invoke_result({"sessionId": "s"})
        assert parsed["stdout"] == ""
        assert parsed["exit_code"] is None

    def test_long_output_is_truncated(self, code_interp):
        huge = "x" * 50_000
        parsed = code_interp.parse_invoke_result(stream_response(ok_event(stdout=huge)))
        assert len(parsed["stdout"]) < len(huge)
        assert "已截断" in parsed["stdout"]
        assert "50000" in parsed["stdout"]


# ---------------------------------------------------------------------------
# 输出格式化
# ---------------------------------------------------------------------------


class TestFormatExecution:
    def test_labels_stdout_and_stderr(self, code_interp):
        text = code_interp.format_execution(
            code_interp.parse_invoke_result(
                stream_response(ok_event(stdout="19485", stderr="DeprecationWarning"))
            )
        )
        assert "STDOUT:" in text and "19485" in text
        assert "STDERR:" in text and "DeprecationWarning" in text

    def test_silent_success_tells_model_to_print(self, code_interp):
        """代码跑通但没输出时,直接说"记得 print",
        否则模型会反复重试同一段没有输出的代码。"""
        text = code_interp.format_execution(
            code_interp.parse_invoke_result(stream_response(ok_event()))
        )
        assert "print()" in text

    def test_failure_is_explicit(self, code_interp):
        text = code_interp.format_execution(
            code_interp.parse_invoke_result(
                stream_response(ok_event(stderr="NameError", exit_code=1, is_error=True))
            )
        )
        assert "执行失败" in text and "exit_code=1" in text

    def test_nonzero_exit_without_error_flag_still_reported(self, code_interp):
        text = code_interp.format_execution(
            code_interp.parse_invoke_result(
                stream_response(ok_event(stdout="部分输出", exit_code=2))
            )
        )
        assert "执行失败" in text

    def test_generated_files_are_mentioned(self, code_interp):
        text = code_interp.format_execution(
            code_interp.parse_invoke_result(
                stream_response(
                    ok_event(
                        stdout="done",
                        resources=[{"type": "resource_link", "name": "chart.png"}],
                    )
                )
            )
        )
        assert "chart.png" in text


# ---------------------------------------------------------------------------
# 惰性会话
# ---------------------------------------------------------------------------


class FakeCodeInterpreter:
    """假沙箱客户端,记录调用。"""

    instances: list["FakeCodeInterpreter"] = []

    def __init__(self, region):
        self.region = region
        self.started_with = None
        self.stopped = False
        self.executed: list[dict] = []
        self.installed: list[list[str]] = []
        self.files: dict[str, object] = {}
        self.execute_response = stream_response(ok_event(stdout="ok\n"))
        FakeCodeInterpreter.instances.append(self)

    def start(self, identifier=None):
        self.started_with = identifier
        return "sess-fake"

    def stop(self):
        self.stopped = True
        return True

    def execute_code(self, code, language="python", clear_context=False):
        self.executed.append(
            {"code": code, "language": language, "clear_context": clear_context}
        )
        return self.execute_response

    def install_packages(self, packages, upgrade=False):
        self.installed.append(list(packages))
        return stream_response(ok_event(stdout="Successfully installed\n"))

    def download_file(self, path):
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]


@pytest.fixture
def fake_sandbox(monkeypatch):
    """把 bedrock_agentcore.tools.CodeInterpreter 换成假实现。"""
    import bedrock_agentcore.tools as tools_pkg

    FakeCodeInterpreter.instances.clear()
    monkeypatch.setattr(tools_pkg, "CodeInterpreter", FakeCodeInterpreter)
    return FakeCodeInterpreter


class TestLazySandbox:
    def test_does_not_start_until_first_use(self, code_interp, fake_sandbox):
        from agent.config import get_settings

        sandbox = code_interp.LazySandbox(get_settings())
        assert sandbox.started is False
        assert fake_sandbox.instances == []

        sandbox.client()
        assert sandbox.started is True
        assert len(fake_sandbox.instances) == 1

    def test_reuses_the_same_session(self, code_interp, fake_sandbox):
        from agent.config import get_settings

        sandbox = code_interp.LazySandbox(get_settings())
        first, second = sandbox.client(), sandbox.client()
        assert first is second
        assert len(fake_sandbox.instances) == 1

    def test_uses_configured_identifier(self, code_interp, fake_sandbox):
        from agent.config import get_settings

        sandbox = code_interp.LazySandbox(get_settings())
        sandbox.client()
        assert fake_sandbox.instances[0].started_with == "aws.codeinterpreter.v1"

    def test_close_is_safe_when_never_started(self, code_interp, fake_sandbox):
        from agent.config import get_settings

        code_interp.LazySandbox(get_settings()).close()
        assert fake_sandbox.instances == []

    def test_close_failure_is_swallowed(self, code_interp, fake_sandbox):
        """关会话失败不该影响对话结果 —— 会话本身有超时兜底。"""
        from agent.config import get_settings

        sandbox = code_interp.LazySandbox(get_settings())
        client = sandbox.client()
        client.stop = lambda: (_ for _ in ()).throw(RuntimeError("网络抖动"))
        sandbox.close()  # 不该抛
        assert sandbox.started is False


class TestSessionLifecycleViaExitStack:
    def test_session_closed_on_stack_exit(self, code_interp, fake_sandbox):
        from agent.config import get_settings

        with contextlib.ExitStack() as stack:
            tools = code_interp.build_code_tools(
                get_settings(), stack, session_id="s1", actor_id="a1"
            )
            run_python = next(t for t in tools if t.tool_name == "run_python")
            run_python(code="print(1)")
            assert fake_sandbox.instances[0].stopped is False

        assert fake_sandbox.instances[0].stopped is True

    def test_session_closed_even_if_body_raises(self, code_interp, fake_sandbox):
        from agent.config import get_settings

        with pytest.raises(ValueError):
            with contextlib.ExitStack() as stack:
                tools = code_interp.build_code_tools(
                    get_settings(), stack, session_id="s1", actor_id="a1"
                )
                next(t for t in tools if t.tool_name == "run_python")(code="print(1)")
                raise ValueError("对话中途失败")

        assert fake_sandbox.instances[0].stopped is True

    def test_unused_sandbox_never_starts(self, code_interp, fake_sandbox):
        """只是组装工具、模型没调用,就不该开会话。"""
        from agent.config import get_settings

        with contextlib.ExitStack() as stack:
            code_interp.build_code_tools(
                get_settings(), stack, session_id="s1", actor_id="a1"
            )
        assert fake_sandbox.instances == []


# ---------------------------------------------------------------------------
# run_python
# ---------------------------------------------------------------------------


@pytest.fixture
def tools(code_interp, fake_sandbox):
    from agent.config import get_settings

    with contextlib.ExitStack() as stack:
        built = code_interp.build_code_tools(
            get_settings(), stack, session_id="sess-1", actor_id="actor-demo"
        )
        yield {t.tool_name: t for t in built}


class TestRunPython:
    def test_passes_code_through(self, tools, fake_sandbox):
        tools["run_python"](code="print(19485/100)")
        assert fake_sandbox.instances[0].executed[0]["code"] == "print(19485/100)"
        assert fake_sandbox.instances[0].executed[0]["language"] == "python"

    def test_reset_maps_to_clear_context(self, tools, fake_sandbox):
        tools["run_python"](code="x=1", reset=True)
        assert fake_sandbox.instances[0].executed[0]["clear_context"] is True

    def test_variables_persist_by_default(self, tools, fake_sandbox):
        tools["run_python"](code="x=1")
        assert fake_sandbox.instances[0].executed[0]["clear_context"] is False

    def test_empty_code_is_refused_without_starting_sandbox(self, tools, fake_sandbox):
        result = tools["run_python"](code="   ")
        assert "不能为空" in str(result)
        assert fake_sandbox.instances == []

    def test_oversized_code_is_refused(self, code_interp, tools, fake_sandbox):
        result = tools["run_python"](code="#" * 30_000)
        assert "过长" in str(result)
        assert fake_sandbox.instances == []

    def test_sandbox_error_is_reported_not_raised(self, tools, fake_sandbox):
        """工具不该抛异常 —— 抛了会中断整个 Agent 循环。
        应该把失败作为文本返回,让模型自己决定怎么办。"""
        tools["run_python"](code="print(1)")
        fake_sandbox.instances[0].execute_response = stream_response(
            {"throttlingException": {"message": "太快了"}}
        )
        result = str(tools["run_python"](code="print(2)"))
        assert "沙箱执行失败" in result and "太快了" in result

    def test_unexpected_exception_does_not_leak_internals(self, tools, fake_sandbox):
        tools["run_python"](code="print(1)")

        def boom(**kwargs):
            raise RuntimeError("内部细节:endpoint=https://secret.internal")

        fake_sandbox.instances[0].execute_code = boom
        result = str(tools["run_python"](code="print(2)"))
        assert "secret.internal" not in result
        assert "RuntimeError" in result


# ---------------------------------------------------------------------------
# publish_file
# ---------------------------------------------------------------------------


class FakeS3:
    def __init__(self):
        self.objects: dict[tuple[str, str], dict] = {}

    def put_object(self, **kwargs):
        self.objects[(kwargs["Bucket"], kwargs["Key"])] = kwargs
        return {}

    def generate_presigned_url(self, op, Params, ExpiresIn):
        return f"https://{Params['Bucket']}.s3.cn-northwest-1.amazonaws.com.cn/{Params['Key']}?X-Amz-Expires={ExpiresIn}"


@pytest.fixture
def s3(monkeypatch):
    import boto3

    fake = FakeS3()
    original = boto3.client

    def patched(service, **kwargs):
        if service == "s3":
            return fake
        return original(service, **kwargs)

    monkeypatch.setattr(boto3, "client", patched)
    return fake


@pytest.fixture
def tools_with_bucket(code_interp, fake_sandbox, monkeypatch):
    from agent.config import get_settings

    settings = get_settings()
    object.__setattr__(settings, "artifact_bucket", "agentcore-cn-artifacts-111122223333")
    with contextlib.ExitStack() as stack:
        built = code_interp.build_code_tools(
            settings, stack, session_id="sess-1", actor_id="actor-demo"
        )
        yield {t.tool_name: t for t in built}
    object.__setattr__(settings, "artifact_bucket", "")


class TestPublishFile:
    def test_uploads_and_returns_presigned_url(self, tools_with_bucket, fake_sandbox, s3):
        tools_with_bucket["run_python"](code="savefig")
        fake_sandbox.instances[0].files["chart.png"] = b"\x89PNG fake"

        result = str(tools_with_bucket["publish_file"](sandbox_path="chart.png"))

        assert "https://" in result
        assert "X-Amz-Expires=3600" in result
        key = ("agentcore-cn-artifacts-111122223333",
               "outputs/actor-demo/sess-1/chart.png")
        assert key in s3.objects
        assert s3.objects[key]["ContentType"] == "image/png"
        assert s3.objects[key]["ServerSideEncryption"] == "AES256"

    def test_key_is_namespaced_by_actor_and_session(self, tools_with_bucket,
                                                    fake_sandbox, s3):
        """S3 key 必须按 actor/session 分区,否则两个用户会互相覆盖产物。"""
        tools_with_bucket["run_python"](code="x")
        fake_sandbox.instances[0].files["out.csv"] = "a,b\n1,2\n"

        tools_with_bucket["publish_file"](sandbox_path="out.csv")

        keys = [k for _, k in s3.objects]
        assert keys == ["outputs/actor-demo/sess-1/out.csv"]

    def test_text_content_is_encoded(self, tools_with_bucket, fake_sandbox, s3):
        tools_with_bucket["run_python"](code="x")
        fake_sandbox.instances[0].files["r.csv"] = "订单,金额\nORD-1024,194.85\n"

        tools_with_bucket["publish_file"](sandbox_path="r.csv")

        body = next(iter(s3.objects.values()))["Body"]
        assert isinstance(body, bytes)
        assert "ORD-1024".encode() in body

    @pytest.mark.parametrize(
        "bad_path",
        [
            "/etc/passwd",
            "../../../etc/passwd",
            "out/../../secret",
            "",
            "   ",
            "file;rm -rf /",
            "a" * 200,
            "$(whoami).png",
        ],
    )
    def test_rejects_unsafe_paths(self, tools_with_bucket, fake_sandbox, s3, bad_path):
        result = str(tools_with_bucket["publish_file"](sandbox_path=bad_path))
        assert "路径" in result
        assert s3.objects == {}

    def test_missing_file_gives_actionable_message(self, tools_with_bucket,
                                                   fake_sandbox, s3):
        tools_with_bucket["run_python"](code="x")
        result = str(tools_with_bucket["publish_file"](sandbox_path="nope.png"))
        assert "没有 nope.png" in result
        assert s3.objects == {}

    def test_without_bucket_configured_it_says_so(self, tools, fake_sandbox):
        result = str(tools["publish_file"](sandbox_path="chart.png"))
        assert "ARTIFACT_BUCKET" in result

    def test_upload_failure_does_not_leak_internals(self, tools_with_bucket,
                                                    fake_sandbox, s3):
        tools_with_bucket["run_python"](code="x")
        fake_sandbox.instances[0].files["chart.png"] = b"png"

        def boom(**kwargs):
            raise RuntimeError("内部细节:access key AKIA...")

        s3.put_object = boom
        result = str(tools_with_bucket["publish_file"](sandbox_path="chart.png"))
        assert "AKIA" not in result
        assert "上传失败" in result

    @pytest.mark.parametrize(
        ("filename", "expected"),
        [
            ("chart.png", "image/png"),
            ("data.csv", "text/csv"),
            ("report.md", "text/markdown"),
            ("x.bin", "application/octet-stream"),
        ],
    )
    def test_content_type_is_inferred(self, tools_with_bucket, fake_sandbox, s3,
                                      filename, expected):
        tools_with_bucket["run_python"](code="x")
        fake_sandbox.instances[0].files[filename] = b"data"
        tools_with_bucket["publish_file"](sandbox_path=filename)
        assert next(iter(s3.objects.values()))["ContentType"] == expected


# ---------------------------------------------------------------------------
# install_packages
# ---------------------------------------------------------------------------


class TestInstallPackages:
    def test_allowlisted_package_is_installed(self, tools, fake_sandbox):
        result = str(tools["install_packages"](packages=["scipy"]))
        assert "已安装" in result
        assert fake_sandbox.instances[0].installed == [["scipy"]]

    def test_version_specifier_is_allowed(self, tools, fake_sandbox):
        tools["install_packages"](packages=["pandas>=2.0"])
        assert fake_sandbox.instances[0].installed == [["pandas>=2.0"]]

    @pytest.mark.parametrize(
        "package", ["requests", "cryptography", "evil-package", "os", "boto3"]
    )
    def test_non_allowlisted_package_is_refused(self, tools, fake_sandbox, package):
        """沙箱有出网能力,不能让模型随便从 PyPI 拉包进来。"""
        result = str(tools["install_packages"](packages=[package]))
        assert "白名单" in result
        assert fake_sandbox.instances == []

    def test_one_bad_package_blocks_the_whole_batch(self, tools, fake_sandbox):
        result = str(tools["install_packages"](packages=["pandas", "requests"]))
        assert "requests" in result
        assert fake_sandbox.instances == []

    def test_empty_list_is_refused(self, tools, fake_sandbox):
        assert "不能为空" in str(tools["install_packages"](packages=[]))
        assert fake_sandbox.instances == []


# ---------------------------------------------------------------------------
# 与 system prompt 的一致性
# ---------------------------------------------------------------------------


class TestPromptAlignment:
    def test_system_prompt_tells_model_to_use_run_python(self):
        from agent.assembly import SYSTEM_PROMPT

        assert "run_python" in SYSTEM_PROMPT

    def test_system_prompt_describes_the_chart_flow(self):
        """出图是两步:run_python 存 PNG -> publish_file 拿链接。
        prompt 里不写清楚,模型会试图直接把图片字节返回给用户。"""
        from agent.assembly import SYSTEM_PROMPT

        assert "publish_file" in SYSTEM_PROMPT

    def test_tool_names_are_stable(self, tools):
        """system prompt 和 Lambda 侧的工具描述都引用了这些名字,不能随便改。"""
        assert set(tools) == {"run_python", "publish_file", "install_packages"}

    def test_run_python_description_mentions_cents(self, tools):
        """金额用整数分计算,这条必须写在工具描述里 ——
        Lambda 的 get_refund_policy 也提了同一条,两边要一致。"""
        description = tools["run_python"].tool_spec["description"]
        assert "分" in description
