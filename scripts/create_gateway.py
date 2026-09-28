#!/usr/bin/env python3
"""创建/更新 AgentCore Gateway 并挂上业务工具 Lambda target。

Gateway 不用 CloudFormation:`AWS::BedrockAgentCore::*` 资源类型在中国区是否
可用尚未实测,而 boto3 的 bedrock-agentcore-control 模型是确定可用的。
这个脚本幂等,可以重复跑。

中国区约束(已在代码里规避):
  * authorizerType 不能是 NONE,必须 CUSTOM_JWT 或 AWS_IAM。这里用自建 IdP 的
    CUSTOM_JWT。
  * protocolConfiguration.mcp.searchType 不能设 SEMANTIC —— 中国区没有语义检索。
    所以这里【完全不传 searchType】,工具靠 description 让模型自己选。
  * ARN 分区是 aws-cn。

用法:
    python scripts/create_gateway.py                 # 创建或更新
    python scripts/create_gateway.py --show          # 只打印当前状态
    python scripts/create_gateway.py --delete-target # 删掉 target 重建(改 schema 后用)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import ClientError

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src" / "lambdas" / "tools"))

# handler 在 import 时要读这个环境变量,但这里只用 TOOL_SCHEMA,给占位值即可
os.environ.setdefault("BUSINESS_TABLE", "unused-by-create-gateway")

from tools_handler import TOOL_SCHEMA  # noqa: E402

import naming  # noqa: E402

TARGET_NAME = "business"
# Gateway 对外暴露的工具名会变成 "<TARGET_NAME>___<tool_name>"
TOOL_NAME_PREFIX = f"{TARGET_NAME}___"

READY_STATES = {"READY"}
FAILED_STATES = {"FAILED", "UPDATE_UNSUCCESSFUL", "SYNCHRONIZE_UNSUCCESSFUL"}


def log(msg: str) -> None:
    print(f"\033[1;34m==>\033[0m {msg}")


def warn(msg: str) -> None:
    print(f"\033[1;33m[!]\033[0m {msg}", file=sys.stderr)


def die(msg: str) -> "None":
    print(f"\033[1;31m[x]\033[0m {msg}", file=sys.stderr)
    raise SystemExit(1)


# ---------------------------------------------------------------------------
# CloudFormation 输出读取
# ---------------------------------------------------------------------------


def stack_outputs(cfn, stack_name: str) -> dict[str, str]:
    try:
        stacks = cfn.describe_stacks(StackName=stack_name)["Stacks"]
    except ClientError as exc:
        if "does not exist" in str(exc):
            die(f"栈 {stack_name} 不存在,先跑 scripts/deploy.sh")
        raise
    return {o["OutputKey"]: o["OutputValue"] for o in stacks[0].get("Outputs", [])}


# ---------------------------------------------------------------------------
# Gateway
# ---------------------------------------------------------------------------


def find_gateway(client, name: str) -> dict[str, Any] | None:
    paginator_token = None
    while True:
        kwargs = {"maxResults": 50}
        if paginator_token:
            kwargs["nextToken"] = paginator_token
        resp = client.list_gateways(**kwargs)
        for item in resp.get("items", []):
            if item.get("name") == name:
                return item
        paginator_token = resp.get("nextToken")
        if not paginator_token:
            return None


def build_authorizer_config(discovery_url: str, audience: str, client_ids: list[str]) -> dict:
    """CUSTOM_JWT 配置。

    allowedAudience 必须和自建 IdP 签出来的 aud 一致 —— 不一致的话
    Gateway 会一律拒绝,而且错误信息很不明显。
    """
    cfg: dict[str, Any] = {
        "customJWTAuthorizer": {
            "discoveryUrl": discovery_url,
            "allowedAudience": [audience],
        }
    }
    if client_ids:
        cfg["customJWTAuthorizer"]["allowedClients"] = client_ids
    return cfg


def ensure_gateway(client, *, name: str, role_arn: str, authorizer_config: dict) -> dict:
    existing = find_gateway(client, name)
    common = {
        "roleArn": role_arn,
        "protocolType": "MCP",
        "authorizerType": "CUSTOM_JWT",
        "authorizerConfiguration": authorizer_config,
        "protocolConfiguration": {
            "mcp": {
                # 刻意不传 searchType:中国区没有 SEMANTIC 语义检索
                "instructions": (
                    "售后业务工具。处理订单/物流/工单问题时,先用 get_order 查订单,"
                    "再按需调用其他工具。"
                ),
            }
        },
    }

    if existing:
        log(f"Gateway {name} 已存在({existing['gatewayId']}),更新配置")
        resp = client.update_gateway(
            gatewayIdentifier=existing["gatewayId"],
            name=name,
            description="agentcore-cn demo gateway (after-sales tools)",
            **common,
        )
    else:
        log(f"创建 Gateway {name}")
        resp = client.create_gateway(
            name=name,
            description="agentcore-cn demo gateway (after-sales tools)",
            **common,
        )
    return resp


def wait_ready(fetch, label: str, timeout: int = 300) -> dict:
    """轮询到 READY。失败时把 statusReasons 打出来 —— 这是排障的关键信息。"""
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        last = fetch()
        status = last.get("status", "")
        if status in READY_STATES:
            log(f"{label} 已就绪")
            return last
        if status in FAILED_STATES:
            reasons = last.get("statusReasons") or ["(没有给出原因)"]
            die(f"{label} 进入 {status}:\n  " + "\n  ".join(str(r) for r in reasons))
        print(f"    {label} status={status},等待中…")
        time.sleep(5)
    die(f"{label} 等待超时,最后状态 {last.get('status')}")
    return last


# ---------------------------------------------------------------------------
# Target
# ---------------------------------------------------------------------------


def find_target(client, gateway_id: str, name: str) -> dict[str, Any] | None:
    token = None
    while True:
        kwargs = {"gatewayIdentifier": gateway_id, "maxResults": 50}
        if token:
            kwargs["nextToken"] = token
        resp = client.list_gateway_targets(**kwargs)
        for item in resp.get("items", []):
            if item.get("name") == name:
                return item
        token = resp.get("nextToken")
        if not token:
            return None


def ensure_lambda_target(client, *, gateway_id: str, lambda_arn: str) -> dict:
    target_config = {
        "mcp": {
            "lambda": {
                "lambdaArn": lambda_arn,
                "toolSchema": {"inlinePayload": TOOL_SCHEMA},
            }
        }
    }
    # Gateway 用自己的执行角色调 Lambda
    credential_config = [{"credentialProviderType": "GATEWAY_IAM_ROLE"}]

    existing = find_target(client, gateway_id, TARGET_NAME)
    if existing:
        log(f"Target {TARGET_NAME} 已存在({existing['targetId']}),更新 schema")
        return client.update_gateway_target(
            gatewayIdentifier=gateway_id,
            targetId=existing["targetId"],
            name=TARGET_NAME,
            description="After-sales business tools backed by Lambda + DynamoDB",
            targetConfiguration=target_config,
            credentialProviderConfigurations=credential_config,
        )
    log(f"创建 Target {TARGET_NAME},共 {len(TOOL_SCHEMA)} 个工具")
    return client.create_gateway_target(
        gatewayIdentifier=gateway_id,
        name=TARGET_NAME,
        description="After-sales business tools backed by Lambda + DynamoDB",
        targetConfiguration=target_config,
        credentialProviderConfigurations=credential_config,
    )


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default=os.environ.get("PROJECT", "agentcore-cn"))
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "cn-northwest-1"))
    parser.add_argument("--profile", default=os.environ.get("AWS_PROFILE") or None)
    parser.add_argument("--show", action="store_true", help="只打印当前状态")
    parser.add_argument(
        "--delete-target", action="store_true", help="先删掉 target 再重建"
    )
    args = parser.parse_args()

    session = boto3.Session(profile_name=args.profile, region_name=args.region)

    # 分区检查:这个项目只针对中国区
    partition = session.client("sts").get_caller_identity()["Arn"].split(":")[1]
    if partition != "aws-cn":
        die(f"当前凭证在分区 {partition},不是 aws-cn")

    cfn = session.client("cloudformation")
    control = session.client("bedrock-agentcore-control")

    gateway_name = f"{args.project}-gw"

    if args.show:
        gw = find_gateway(control, gateway_name)
        if not gw:
            log(f"Gateway {gateway_name} 不存在")
            return 0
        detail = control.get_gateway(gatewayIdentifier=gw["gatewayId"])
        print(json.dumps(detail, indent=2, default=str, ensure_ascii=False))
        for target in control.list_gateway_targets(
            gatewayIdentifier=gw["gatewayId"], maxResults=50
        ).get("items", []):
            print(json.dumps(target, indent=2, default=str, ensure_ascii=False))
        return 0

    foundation = stack_outputs(cfn, f"{args.project}-foundation")
    idp = stack_outputs(cfn, f"{args.project}-auth-idp")
    tools = stack_outputs(cfn, f"{args.project}-business-tools")

    role_arn = foundation["GatewayExecutionRoleArn"]
    discovery_url = idp["DiscoveryUrl"]
    lambda_arn = tools["ToolsFunctionArn"]

    if "placeholder.invalid" in discovery_url:
        die("IdP 的 issuer 还是占位值,先跑 scripts/deploy.sh 回填")

    client_ids = naming.all_client_ids(args.project)

    log(f"discoveryUrl = {discovery_url}")
    log(f"allowedAudience = {args.project}")
    log(f"allowedClients = {client_ids}")

    gateway = ensure_gateway(
        control,
        name=gateway_name,
        role_arn=role_arn,
        authorizer_config=build_authorizer_config(discovery_url, args.project, client_ids),
    )
    gateway_id = gateway["gatewayId"]
    gateway = wait_ready(
        lambda: control.get_gateway(gatewayIdentifier=gateway_id), f"Gateway {gateway_id}"
    )

    if args.delete_target:
        existing = find_target(control, gateway_id, TARGET_NAME)
        if existing:
            log(f"删除已有 target {existing['targetId']}")
            control.delete_gateway_target(
                gatewayIdentifier=gateway_id, targetId=existing["targetId"]
            )
            time.sleep(5)

    target = ensure_lambda_target(control, gateway_id=gateway_id, lambda_arn=lambda_arn)
    target_id = target["targetId"]
    wait_ready(
        lambda: control.get_gateway_target(
            gatewayIdentifier=gateway_id, targetId=target_id
        ),
        f"Target {target_id}",
    )

    gateway_url = gateway["gatewayUrl"]
    print()
    log("Gateway 就绪。把下面这行加进 Runtime 的环境变量:")
    print(f"\n  GATEWAY_URL={gateway_url}\n")
    log("暴露给模型的工具名(带 target 前缀):")
    for tool in TOOL_SCHEMA:
        print(f"  {TOOL_NAME_PREFIX}{tool['name']}")
    print()
    log("自测:先拿 token 再调 MCP tools/list")
    print(
        f"""
  TOKEN=$(curl -s -u '<client_id>:<secret>' \\
    -d 'grant_type=client_credentials' \\
    {idp['TokenEndpoint']} | python3 -c 'import json,sys;print(json.load(sys.stdin)["access_token"])')

  curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \\
    -d '{{"jsonrpc":"2.0","id":1,"method":"tools/list"}}' \\
    {gateway_url} | python3 -m json.tool
"""
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
