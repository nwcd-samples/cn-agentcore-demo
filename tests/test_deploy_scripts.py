"""Runtime 控制面与调用参数的离线验证。

和 test_gateway_wiring / test_identity 一样,用 botocore 的 ParamValidator
按真实服务模型校验参数形状 —— 不需要凭证。

另外有一组针对中国区约束的断言:这些字段传错了要么建栈失败,
要么部署成功但行为不对(比如忘了 runtimeUserId 导致 Identity 出向失败)。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import boto3
import pytest
from botocore.validate import ParamValidator

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
os.environ.setdefault("BUSINESS_TABLE", "unused")

IMAGE = (
    "111122223333.dkr.ecr.cn-northwest-1.amazonaws.com.cn/agentcore-cn/agent:abc123"
)
ROLE_ARN = "arn:aws-cn:iam::111122223333:role/agentcore-cn-runtime-exec"
RUNTIME_ARN = (
    "arn:aws-cn:bedrock-agentcore:cn-northwest-1:111122223333:runtime/agentcore_cn_agent-x"
)
DISCOVERY_URL = (
    "https://abc.execute-api.cn-northwest-1.amazonaws.com.cn"
    "/.well-known/openid-configuration"
)


@pytest.fixture(scope="module")
def control_model():
    return (
        boto3.Session()
        .client(
            "bedrock-agentcore-control",
            region_name="cn-northwest-1",
            aws_access_key_id="testing",
            aws_secret_access_key="testing",
        )
        .meta.service_model
    )


@pytest.fixture(scope="module")
def data_model():
    return (
        boto3.Session()
        .client(
            "bedrock-agentcore",
            region_name="cn-northwest-1",
            aws_access_key_id="testing",
            aws_secret_access_key="testing",
        )
        .meta.service_model
    )


@pytest.fixture(scope="module")
def create_runtime():
    import create_runtime as module

    return module


def assert_valid(model, operation: str, params: dict) -> None:
    report = ParamValidator().validate(
        params, model.operation_model(operation).input_shape
    )
    if report.has_errors():
        raise AssertionError(f"{operation} 参数不合法:\n{report.generate_report()}")


def make_params(create_runtime, auth="jwt", **overrides):
    authorizer = create_runtime.build_authorizer(
        auth, "agentcore-cn", {"DiscoveryUrl": DISCOVERY_URL}
    )
    params = create_runtime.build_create_params(
        name="agentcore_cn_agent",
        image=IMAGE,
        role_arn=ROLE_ARN,
        environment={"PROJECT": "agentcore-cn", "AWS_REGION": "cn-northwest-1"},
        authorizer=authorizer,
    )
    params.update(overrides)
    return params


# ---------------------------------------------------------------------------
# CreateAgentRuntime
# ---------------------------------------------------------------------------


class TestCreateRuntimeParams:
    def test_params_validate(self, control_model, create_runtime):
        assert_valid(control_model, "CreateAgentRuntime", make_params(create_runtime))

    def test_iam_variant_validates(self, control_model, create_runtime):
        assert_valid(
            control_model, "CreateAgentRuntime", make_params(create_runtime, auth="iam")
        )

    def test_update_accepts_the_same_shape(self, control_model, create_runtime):
        params = make_params(create_runtime)
        params.pop("agentRuntimeName")
        params["agentRuntimeId"] = "agentcore_cn_agent-abc"
        assert_valid(control_model, "UpdateAgentRuntime", params)

    def test_container_artifact_is_used(self, create_runtime):
        params = make_params(create_runtime)
        assert params["agentRuntimeArtifact"]["containerConfiguration"][
            "containerUri"
        ] == IMAGE

    def test_no_capacity_provider_config(self, create_runtime):
        """中国区只支持 MicroVM。capacityProviderConfiguration 是给
        Managed EC2 用的,传了会失败。"""
        assert "capacityProviderConfiguration" not in make_params(create_runtime)

    def test_no_filesystem_config(self, create_runtime):
        """中国区不支持 S3 Files 形式的 BYO 文件系统。"""
        assert "filesystemConfigurations" not in make_params(create_runtime)

    def test_network_mode_is_public(self, create_runtime):
        """容器要出网访问 api.deepseek.com,必须 PUBLIC。"""
        params = make_params(create_runtime)
        assert params["networkConfiguration"]["networkMode"] == "PUBLIC"

    def test_server_protocol_is_http(self, create_runtime):
        """容器暴露的是 /invocations + /ping,不是 MCP server。"""
        params = make_params(create_runtime)
        assert params["protocolConfiguration"]["serverProtocol"] == "HTTP"

    def test_jwt_authorizer_uses_self_hosted_idp(self, create_runtime):
        params = make_params(create_runtime)
        jwt = params["authorizerConfiguration"]["customJWTAuthorizer"]
        assert jwt["discoveryUrl"] == DISCOVERY_URL
        assert jwt["allowedAudience"] == ["agentcore-cn"]

    def test_allowed_clients_match_runtime_callers_only(self, create_runtime):
        """Quick 只接 business MCP Gateway,不能因此获得 Runtime 入向权限。"""
        import naming

        params = make_params(create_runtime)
        jwt = params["authorizerConfiguration"]["customJWTAuthorizer"]
        assert jwt["allowedClients"] == naming.runtime_client_ids("agentcore-cn")
        assert naming.quick_client_id("agentcore-cn") not in jwt["allowedClients"]

    def test_iam_mode_omits_authorizer_entirely(self, create_runtime):
        """SigV4 入向的表达方式是"不传 authorizerConfiguration",
        不是传一个空对象。"""
        params = make_params(create_runtime, auth="iam")
        assert "authorizerConfiguration" not in params

    def test_placeholder_issuer_is_refused(self, create_runtime):
        """issuer 还是占位值就建 Runtime,会得到一个永远 403 的端点。"""
        with pytest.raises(SystemExit):
            create_runtime.build_authorizer(
                "jwt", "agentcore-cn", {"DiscoveryUrl": "https://placeholder.invalid/x"}
            )

    def test_arns_are_china_partition(self):
        for arn in (ROLE_ARN, RUNTIME_ARN):
            assert arn.split(":")[1] == "aws-cn"
        assert ".amazonaws.com.cn" in IMAGE


# ---------------------------------------------------------------------------
# 环境变量
# ---------------------------------------------------------------------------


class TestEnvironment:
    def _env(self, create_runtime, extra=None):
        return create_runtime.build_environment(
            "agentcore-cn",
            "cn-northwest-1",
            {"ArtifactBucketName": "agentcore-cn-artifacts-111122223333"},
            extra or {},
        )

    def test_matches_what_the_agent_actually_reads(self, create_runtime):
        """脚本注入的变量名必须真的被 Agent 读取,否则等于没配 ——
        Agent 会静默用默认值,而部署方以为配上了。

        扫 config.py 和 main.py 两个文件:前者读业务配置,
        后者读 LOG_LEVEL / PORT 这类进程级配置。

        OTEL_* 例外:那些是给 aws-opentelemetry-distro / OTEL SDK 读的,
        我们的代码不碰。它们在 create_runtime.py 里各自带了注释说明用途。
        """
        sources = "\n".join(
            (REPO_ROOT / "src" / "agent" / f).read_text()
            for f in ("config.py", "main.py")
        )
        unread = [
            k
            for k in self._env(create_runtime)
            if f'"{k}"' not in sources and not k.startswith("OTEL_")
        ]
        assert not unread, f"注入了但没人读的环境变量:{unread}"

    def test_otel_vars_are_explained(self, create_runtime):
        """OTEL_* 绕过了上面的检查,所以必须在源码里写清为什么注入它们 ——
        否则后人看到一个没人读的变量会顺手删掉。
        """
        source = (REPO_ROOT / "scripts" / "create_runtime.py").read_text()
        for key in (k for k in self._env(create_runtime) if k.startswith("OTEL_")):
            assert key in source
        # 这两个是规避中国区 SDK bug 的,理由必须写明
        assert "logs.cn-northwest-1.amazonaws.com" in source or "aws-cn" in source
        assert "OTEL_LOGS_EXPORTER" in source

    def test_all_otlp_exporters_are_disabled(self, create_runtime):
        """aws-opentelemetry-distro 在中国区把 logs / xray endpoint 都拼成
        .amazonaws.com(漏了 .cn,DNS 验证不存在)。解析失败后它在后台反复
        重试,把请求线程拖住 —— 实测 8 个自检步骤 3 秒跑完,调用又挂 4 分钟
        到客户端读超时,业务成功了却看起来失败。

        试过显式设正确的 .com.cn endpoint,换成 403 Forbidden ——
        X-Ray 的 OTLP 端点要 SigV4,手工设 endpoint 绕过了 distro 的签名。
        所以只能关掉导出。
        """
        env = create_runtime.build_otel_environment("cn-northwest-1")
        for key in ("OTEL_TRACES_EXPORTER", "OTEL_LOGS_EXPORTER",
                    "OTEL_METRICS_EXPORTER"):
            assert env[key] == "none", f"{key} 没关掉,会拖死请求线程"

    def test_no_manual_endpoint_override(self, create_runtime):
        """不能手工设 OTLP endpoint —— 那会绕过 distro 的 SigV4 签名,
        换来 403 而不是解决问题。"""
        env = create_runtime.build_otel_environment("cn-northwest-1")
        assert not any("ENDPOINT" in k for k in env), (
            "手工设 endpoint 会绕过 SigV4 签名,实测得到 403"
        )

    def test_export_timeout_is_bounded(self, create_runtime):
        """万一哪天重新打开导出,也不许无限期拖住请求线程。"""
        env = create_runtime.build_otel_environment("cn-northwest-1")
        assert int(env["OTEL_BSP_EXPORT_TIMEOUT"]) <= 10_000

    def test_trace_context_still_works(self, create_runtime):
        """关的只是【导出】。instrumentation 仍在装,所以 trace id / span id
        照常生成并出现在日志里 —— 自检的 Observability 一项靠它通过。
        """
        env = create_runtime.build_otel_environment("cn-northwest-1")
        # 没有关掉 instrumentation 本身的开关
        assert "OTEL_SDK_DISABLED" not in env

    def test_otel_logs_exporter_is_disabled(self, create_runtime):
        """aws-opentelemetry-distro 的 logs exporter 在中国区把 endpoint
        拼成 logs.<region>.amazonaws.com(漏了 .cn),该域名无法解析。
        它会在后台不断重试 DNS,把请求线程拖死 —— 症状是工具调用"卡住"
        而不是报错,调用方一路读超时。

        容器日志本来就通过 stdout 进 CloudWatch,不需要再走一遍 OTLP。
        """
        env = self._env(create_runtime)
        assert env.get("OTEL_LOGS_EXPORTER") == "none"

    def test_the_check_would_catch_a_typo(self, create_runtime):
        """反向对照:确认上面那条不是永远通过。"""
        sources = "\n".join(
            (REPO_ROOT / "src" / "agent" / f).read_text()
            for f in ("config.py", "main.py")
        )
        assert '"BUSINESS_TABLE"' in sources
        assert '"BUSINES_TABLE"' not in sources  # 少一个 S 的拼写错误应被发现

    def test_no_secrets_in_environment(self, create_runtime):
        """密钥一律走 Identity,不进环境变量 ——
        环境变量会出现在控制面 API 响应和 CloudTrail 里。"""
        env = self._env(create_runtime)
        for key in env:
            assert "SECRET" not in key.upper()
            assert not (key.upper().endswith("_KEY") and "PROVIDER" not in key.upper()), (
                f"{key} 看起来像在直接传密钥"
            )
        assert "DEEPSEEK_API_KEY" not in env

    def test_empty_values_are_dropped(self, create_runtime):
        """服务端会拒绝空字符串环境变量。"""
        env = self._env(create_runtime, {"GATEWAY_URL": "", "LOGISTICS_URL": ""})
        assert "GATEWAY_URL" not in env
        assert "LOGISTICS_URL" not in env

    def test_extra_values_are_included(self, create_runtime):
        env = self._env(
            create_runtime, {"GATEWAY_URL": "https://gw.example/mcp"}
        )
        assert env["GATEWAY_URL"] == "https://gw.example/mcp"

    def test_provider_names_are_passed(self, create_runtime):
        env = self._env(create_runtime)
        assert env["DEEPSEEK_API_KEY_PROVIDER"] == "agentcore-cn-deepseek"
        assert env["GATEWAY_OAUTH_PROVIDER"] == "agentcore-cn-gateway-oauth"


# ---------------------------------------------------------------------------
# 端点(灰度)
# ---------------------------------------------------------------------------


class TestEndpointParams:
    def test_create_endpoint_validates(self, control_model):
        assert_valid(
            control_model,
            "CreateAgentRuntimeEndpoint",
            {
                "agentRuntimeId": "agentcore_cn_agent-abc",
                "name": "stable",
                "agentRuntimeVersion": "1",
                "description": "Pinned endpoint",
            },
        )

    def test_update_endpoint_validates(self, control_model):
        assert_valid(
            control_model,
            "UpdateAgentRuntimeEndpoint",
            {
                "agentRuntimeId": "agentcore_cn_agent-abc",
                "endpointName": "stable",
                "agentRuntimeVersion": "2",
            },
        )

    def test_stable_endpoint_name_is_not_reserved(self, create_runtime):
        """DEFAULT 是服务自动建的,自定义端点不能叫这个。"""
        assert create_runtime.STABLE_ENDPOINT != "DEFAULT"


# ---------------------------------------------------------------------------
# InvokeAgentRuntime
# ---------------------------------------------------------------------------


class TestInvokeParams:
    def _kwargs(self, jwt_token=""):
        import invoke as invoke_mod

        class Recorder:
            def __init__(self):
                self.kwargs = None

            def invoke_agent_runtime(self, **kwargs):
                self.kwargs = kwargs
                return {}

        client = Recorder()
        invoke_mod.invoke(
            client,
            arn=RUNTIME_ARN,
            payload={"mode": "sync", "prompt": "x"},
            session_id="demo-" + "a" * 32,
            user_id="demo-user",
            qualifier="DEFAULT",
            jwt_token=jwt_token,
        )
        return client.kwargs

    def test_sigv4_params_validate(self, data_model):
        assert_valid(data_model, "InvokeAgentRuntime", self._kwargs())

    def test_jwt_params_validate(self, data_model):
        assert_valid(data_model, "InvokeAgentRuntime", self._kwargs(jwt_token="tok"))

    def test_sigv4_must_send_runtime_user_id(self):
        """没有它容器里拿不到 workload access token,Identity 出向直接失败。
        这个错误的症状("Workload access token has not been set")
        和调用参数看不出关联,极难查。"""
        assert self._kwargs()["runtimeUserId"] == "demo-user"

    def test_jwt_mode_omits_runtime_user_id(self):
        """CUSTOM_JWT 入向时身份从 JWT 推导,不该再传 runtimeUserId。"""
        assert "runtimeUserId" not in self._kwargs(jwt_token="tok")

    def test_session_id_meets_minimum_length(self):
        """AgentCore 要求 runtimeSessionId 至少 33 个字符。"""
        import invoke as invoke_mod

        session_id = self._kwargs()["runtimeSessionId"]
        assert len(session_id) >= invoke_mod._MIN_SESSION_ID_LEN

    def test_generated_session_ids_are_long_enough(self, tmp_path, monkeypatch):
        import invoke as invoke_mod

        monkeypatch.setattr(invoke_mod, "SESSION_FILE", tmp_path / "sess")
        session_id = invoke_mod.resolve_session_id(new_session=True)
        assert len(session_id) >= invoke_mod._MIN_SESSION_ID_LEN

    def test_session_is_reused_across_calls(self, tmp_path, monkeypatch):
        """同一个 session id 才能续上对话。"""
        import invoke as invoke_mod

        monkeypatch.setattr(invoke_mod, "SESSION_FILE", tmp_path / "sess")
        first = invoke_mod.resolve_session_id(new_session=True)
        assert invoke_mod.resolve_session_id(new_session=False) == first
        assert invoke_mod.resolve_session_id(new_session=True) != first

    def test_payload_is_json_bytes(self):
        import json as _json

        payload = self._kwargs()["payload"]
        assert isinstance(payload, bytes)
        assert _json.loads(payload)["mode"] == "sync"


# ---------------------------------------------------------------------------
# SSE 解析
# ---------------------------------------------------------------------------


class TestSseParsing:
    def test_text_frames_are_concatenated(self, capsys):
        import invoke as invoke_mod

        body = (
            'data: {"type":"tool","name":"business___get_order"}\n\n'
            'data: {"type":"text","delta":"订单"}\n\n'
            'data: {"type":"text","delta":"已签收"}\n\n'
            'data: {"type":"done","sessionId":"s1"}\n\n'
        )
        invoke_mod.print_sse(body)
        out = capsys.readouterr().out
        assert "订单已签收" in out
        assert "business___get_order" in out

    def test_error_frame_is_surfaced(self, capsys):
        import invoke as invoke_mod

        invoke_mod.print_sse('data: {"type":"error","message":"模型超时"}\n\n')
        assert "模型超时" in capsys.readouterr().out

    def test_non_json_frames_do_not_crash(self, capsys):
        import invoke as invoke_mod

        invoke_mod.print_sse("data: not json\n\n")
        assert "not json" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 与 selftest 输出的对接
# ---------------------------------------------------------------------------


class TestSelftestRendering:
    def test_renders_every_step(self, capsys):
        import invoke as invoke_mod

        invoke_mod.print_selftest(
            {
                "summary": {"total": 2, "ok": 1, "failed": 1, "skipped": 0},
                "steps": [
                    {"component": "Runtime", "status": "ok", "duration_ms": 3,
                     "detail": "python 3.13"},
                    {"component": "Gateway", "status": "failed", "duration_ms": 120,
                     "error": "AccessDenied"},
                ],
                "traceId": "abc123",
                "reportUrl": "https://s3.example/report.md",
            }
        )
        out = capsys.readouterr().out
        assert "1/2 项通过" in out
        assert "Runtime" in out and "python 3.13" in out
        assert "Gateway" in out and "AccessDenied" in out
        assert "abc123" in out
        assert "https://s3.example/report.md" in out

    def test_field_names_match_the_report_schema(self):
        """invoke.py 渲染用的字段必须和 selftest.Report.to_dict() 产出的一致。"""
        sys.path.insert(0, str(REPO_ROOT / "src"))
        from agent.selftest import Report, StepResult

        report = Report(
            session_id="s", actor_id="a", region="cn-northwest-1",
            project="p", started_at=1,
        )
        report.steps.append(
            StepResult(name="n", component="Runtime", ok=True, duration_ms=1)
        )
        payload = report.to_dict()
        assert set(payload["summary"]) >= {"total", "ok", "failed", "skipped"}
        assert set(payload["steps"][0]) >= {
            "component", "status", "duration_ms", "detail", "error"
        }


class TestAuthorizationHeaderForwarding:
    """AgentCore 验完 CUSTOM_JWT 后不会把原始 Authorization 头透给容器 ——
    容器只收到 baggage 和一个不透明的 WorkloadAccessToken(实测 2895 字符、
    单段、非 JWT,没有 API 能反解出身份)。

    不配 requestHeaderAllowlist 的后果:identity.py 找不到 Authorization,
    一律回落 anonymous,所有用户的长期记忆挤在 ACTOR#anonymous 一个分区里。
    数据隔离静默失效,不报错、不告警。
    """

    def _params(self, create_runtime):
        return make_params(create_runtime)

    def test_authorization_is_allowlisted(self, create_runtime):
        params = self._params(create_runtime)
        allowlist = params["requestHeaderConfiguration"]["requestHeaderAllowlist"]
        assert "Authorization" in allowlist

    def test_params_still_validate(self, control_model, create_runtime):
        assert_valid(control_model, "CreateAgentRuntime", self._params(create_runtime))

    def test_reason_is_documented(self):
        """这个配置看起来可有可无,必须写清为什么 —— 否则后人会删掉它,
        而删掉之后没有任何报错,只是所有用户的数据悄悄混到一起。"""
        source = (REPO_ROOT / "scripts" / "create_runtime.py").read_text()
        assert "anonymous" in source
        assert "WorkloadAccessToken" in source

    def test_identity_module_reads_that_header(self):
        """allowlist 放行的头名必须和 identity.py 实际查找的一致。"""
        source = (REPO_ROOT / "src" / "agent" / "identity.py").read_text()
        assert 'key.lower() == "authorization"' in source
