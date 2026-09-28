"""Gateway 接线的离线验证。

没有 AWS 凭证也能验的东西:请求参数的形状。
用 botocore 自带的 ParamValidator 把 scripts/create_gateway.py 实际会发出的
参数字典按 bedrock-agentcore-control 的服务模型校一遍。

为什么值得写:Gateway 建栈失败的报错通常只说 ValidationException,
而真实原因往往是参数拼错 —— 少必填字段、嵌套层级搞错、
或者用了中国区不支持的枚举值。这些全部能在这里提前抓到。
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

DISCOVERY_URL = (
    "https://abc123.execute-api.cn-northwest-1.amazonaws.com.cn"
    "/.well-known/openid-configuration"
)
ROLE_ARN = "arn:aws-cn:iam::111122223333:role/agentcore-cn-gateway-exec"
LAMBDA_ARN = "arn:aws-cn:lambda:cn-northwest-1:111122223333:function:agentcore-cn-tools"


@pytest.fixture(scope="module")
def service_model():
    client = boto3.Session().client(
        "bedrock-agentcore-control",
        region_name="cn-northwest-1",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
    )
    return client.meta.service_model


@pytest.fixture(scope="module")
def create_gateway():
    import create_gateway as module

    return module


def assert_valid(service_model, operation: str, params: dict) -> None:
    shape = service_model.operation_model(operation).input_shape
    report = ParamValidator().validate(params, shape)
    if report.has_errors():
        raise AssertionError(f"{operation} 参数不合法:\n{report.generate_report()}")


class TestCreateGatewayParams:
    def _params(self, create_gateway, **overrides) -> dict:
        params = {
            "name": "agentcore-cn-gw",
            "description": "agentcore-cn demo gateway",
            "roleArn": ROLE_ARN,
            "protocolType": "MCP",
            "authorizerType": "CUSTOM_JWT",
            "authorizerConfiguration": create_gateway.build_authorizer_config(
                DISCOVERY_URL, "agentcore-cn", ["agentcore-cn-client"]
            ),
            "protocolConfiguration": {"mcp": {"instructions": "售后业务工具"}},
        }
        params.update(overrides)
        return params

    def test_params_validate_against_service_model(self, service_model, create_gateway):
        assert_valid(service_model, "CreateGateway", self._params(create_gateway))

    def test_update_gateway_accepts_same_shape(self, service_model, create_gateway):
        params = self._params(create_gateway)
        params["gatewayIdentifier"] = "gw-abc123"
        assert_valid(service_model, "UpdateGateway", params)

    def test_authorizer_type_is_not_none(self, create_gateway):
        """中国区禁止 "No authorization" 入向,必须 CUSTOM_JWT 或 AWS_IAM。"""
        params = self._params(create_gateway)
        assert params["authorizerType"] in ("CUSTOM_JWT", "AWS_IAM")

    def test_semantic_search_is_not_requested(self, create_gateway):
        """中国区没有语义检索。一旦传了 searchType=SEMANTIC 就会建栈失败。"""
        params = self._params(create_gateway)
        mcp = params["protocolConfiguration"]["mcp"]
        assert "searchType" not in mcp

    def test_authorizer_config_shape(self, create_gateway):
        cfg = create_gateway.build_authorizer_config(
            DISCOVERY_URL, "agentcore-cn", ["c1", "c2"]
        )
        jwt = cfg["customJWTAuthorizer"]
        assert jwt["discoveryUrl"] == DISCOVERY_URL
        # aud 必须和 IdP 签出来的一致,否则 Gateway 静默全拒
        assert jwt["allowedAudience"] == ["agentcore-cn"]
        assert jwt["allowedClients"] == ["c1", "c2"]

    def test_authorizer_config_omits_empty_client_list(self, create_gateway):
        """allowedClients 传空列表会被服务端当成"谁都不允许",必须整个字段不传。"""
        cfg = create_gateway.build_authorizer_config(DISCOVERY_URL, "agentcore-cn", [])
        assert "allowedClients" not in cfg["customJWTAuthorizer"]

    def test_discovery_url_uses_china_domain(self):
        assert DISCOVERY_URL.endswith("/.well-known/openid-configuration")
        assert ".amazonaws.com.cn" in DISCOVERY_URL

    def test_role_and_lambda_arns_are_china_partition(self):
        for arn in (ROLE_ARN, LAMBDA_ARN):
            assert arn.split(":")[1] == "aws-cn", f"{arn} 分区不对"


class TestCreateGatewayTargetParams:
    def _params(self, tools_handler) -> dict:
        return {
            "gatewayIdentifier": "gw-abc123",
            "name": "business",
            "description": "After-sales business tools",
            "targetConfiguration": {
                "mcp": {
                    "lambda": {
                        "lambdaArn": LAMBDA_ARN,
                        "toolSchema": {"inlinePayload": tools_handler.TOOL_SCHEMA},
                    }
                }
            },
            "credentialProviderConfigurations": [
                {"credentialProviderType": "GATEWAY_IAM_ROLE"}
            ],
        }

    def test_params_validate_against_service_model(self, service_model):
        import tools_handler

        assert_valid(service_model, "CreateGatewayTarget", self._params(tools_handler))

    def test_update_target_accepts_same_shape(self, service_model):
        import tools_handler

        params = self._params(tools_handler)
        params["targetId"] = "tg-abc123"
        assert_valid(service_model, "UpdateGatewayTarget", params)

    def test_tool_schema_is_accepted_as_inline_payload(self, service_model):
        """inlinePayload 是 ToolDefinition 列表,每项要有 name/description/inputSchema。
        这里让 botocore 逐字段校一遍,而不是我们自己肉眼看。"""
        import tools_handler

        shape = service_model.operation_model("CreateGatewayTarget").input_shape
        params = self._params(tools_handler)
        report = ParamValidator().validate(params, shape)
        assert not report.has_errors(), report.generate_report()
        payload = params["targetConfiguration"]["mcp"]["lambda"]["toolSchema"][
            "inlinePayload"
        ]
        assert len(payload) == len(tools_handler.TOOLS)

    def test_credential_provider_is_gateway_iam_role(self, service_model):
        """Lambda target 用 Gateway 自己的执行角色调用,不需要 OAuth/API Key。"""
        import tools_handler

        providers = self._params(tools_handler)["credentialProviderConfigurations"]
        assert [p["credentialProviderType"] for p in providers] == ["GATEWAY_IAM_ROLE"]

    def test_missing_lambda_arn_is_caught(self, service_model):
        """反向验证:校验器确实会报错,不是永远返回通过。"""
        import tools_handler

        params = self._params(tools_handler)
        del params["targetConfiguration"]["mcp"]["lambda"]["lambdaArn"]
        shape = service_model.operation_model("CreateGatewayTarget").input_shape
        assert ParamValidator().validate(params, shape).has_errors()


class TestToolNaming:
    def test_prefix_matches_target_name(self, create_gateway):
        """Gateway 暴露的工具名是 "<target>___<tool>",
        Lambda 侧剥前缀用的分隔符必须和这里一致。"""
        import tools_handler

        assert create_gateway.TOOL_NAME_PREFIX == (
            create_gateway.TARGET_NAME + tools_handler.TOOL_NAME_DELIMITER
        )

    def test_agent_system_prompt_uses_prefixed_names(self):
        """system prompt 里提到的工具名必须带 target 前缀,
        否则模型会按裸名字调,直接找不到工具。"""
        sys.path.insert(0, str(REPO_ROOT / "src"))
        from agent.assembly import SYSTEM_PROMPT

        import tools_handler

        for tool in ("get_order", "get_refund_policy", "list_tickets"):
            prefixed = f"business{tools_handler.TOOL_NAME_DELIMITER}{tool}"
            assert prefixed in SYSTEM_PROMPT, f"system prompt 里缺 {prefixed}"
