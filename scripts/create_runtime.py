#!/usr/bin/env python3
"""创建/更新 AgentCore Runtime,并管理版本与命名端点。

版本与端点这套机制就是 demo 里"灰度发布"能力的落点:
  * 每次 UpdateAgentRuntime 都会产生一个新版本号
  * DEFAULT 端点始终指向最新版本
  * 另外建一个命名端点(默认 stable),手工指定它指向哪个版本
  -> 调用时用 qualifier 选端点,就能让一部分流量走旧版本

中国区约束(已在代码里规避):
  * 只支持 MicroVM capacity provider,所以【不传 capacityProviderConfiguration】
    (那个字段是给 Managed EC2 用的)
  * 不支持 S3 Files 形式的 BYO 文件系统,所以不传 filesystemConfigurations
  * 入向鉴权不能用 Cognito,这里用自建 IdP 的 CUSTOM_JWT;
    也支持 --auth iam 切成 SigV4
  * ARN 分区是 aws-cn

用法:
    python scripts/create_runtime.py --image <ecr-uri>:<tag>
    python scripts/create_runtime.py --show
    python scripts/create_runtime.py --promote 3        # 让 stable 端点指向版本 3
    python scripts/create_runtime.py --auth iam --image ...
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
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import naming  # noqa: E402

# 命名端点的名字。DEFAULT 端点是服务自动建的,始终跟最新版本。
STABLE_ENDPOINT = "stable"

READY_STATES = {"READY"}
FAILED_STATES = {"CREATE_FAILED", "UPDATE_FAILED", "FAILED"}


def log(msg: str) -> None:
    print(f"\033[1;34m==>\033[0m {msg}")


def warn(msg: str) -> None:
    print(f"\033[1;33m[!]\033[0m {msg}", file=sys.stderr)


def die(msg: str) -> None:
    print(f"\033[1;31m[x]\033[0m {msg}", file=sys.stderr)
    raise SystemExit(1)


def stack_outputs(cfn, stack: str) -> dict[str, str]:
    try:
        return {
            o["OutputKey"]: o["OutputValue"]
            for o in cfn.describe_stacks(StackName=stack)["Stacks"][0].get("Outputs", [])
        }
    except ClientError as exc:
        if "does not exist" in str(exc):
            die(f"栈 {stack} 不存在,先跑 ./scripts/deploy.sh")
        raise


# ---------------------------------------------------------------------------
# 配置组装
# ---------------------------------------------------------------------------


def build_environment(
    project: str, region: str, foundation: dict[str, str], extra: dict[str, str]
) -> dict[str, str]:
    """Runtime 容器的环境变量。密钥一个都不放这里 —— 走 Identity。"""
    env = {
        "PROJECT": project,
        "AWS_REGION": region,
        "BUSINESS_TABLE": f"{project}-business",
        "MEMORY_TABLE": f"{project}-memory",
        "ARTIFACT_BUCKET": foundation.get("ArtifactBucketName", ""),
        "DEEPSEEK_API_KEY_PROVIDER": os.environ.get(
            "DEEPSEEK_API_KEY_PROVIDER", f"{project}-deepseek"
        ),
        "GATEWAY_OAUTH_PROVIDER": os.environ.get(
            "GATEWAY_OAUTH_PROVIDER", f"{project}-gateway-oauth"
        ),
        "DEEPSEEK_BASE_URL": os.environ.get(
            "DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"
        ),
        "DEEPSEEK_MODEL": os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
        "LOG_LEVEL": os.environ.get("LOG_LEVEL", "INFO"),
        # 【中国区 SDK bug 规避】aws-opentelemetry-distro 的 logs exporter
        # 把 endpoint 拼成 logs.<region>.amazonaws.com,漏了 aws-cn 需要的
        # .cn 后缀。实测该域名无法解析:
        #   Failed to resolve 'logs.cn-northwest-1.amazonaws.com'
        # 它会在后台不断重试 DNS,把请求线程拖死 —— 症状是工具调用"卡住"
        # 而不是报错,客户端一路读超时。
        #
        # 关掉 OTLP 日志导出即可:容器日志本来就通过 stdout 进
        # /aws/bedrock-agentcore/runtimes/*,不需要再走一遍 OTLP。
        # trace 不受影响(走的是另一个 exporter,域名是对的),
        # 自检里 Observability 一项仍然通过。
        "OTEL_LOGS_EXPORTER": "none",
        "OTEL_PYTHON_LOGGING_AUTO_INSTRUMENTATION_ENABLED": "false",
    }
    env.update({k: v for k, v in extra.items() if v})
    # 空值不传 —— 服务端会拒绝空字符串环境变量
    return {k: v for k, v in env.items() if v}


def build_authorizer(auth: str, project: str, idp: dict[str, str]) -> dict[str, Any] | None:
    """CUSTOM_JWT 用自建 IdP;iam 模式返回 None(不传该字段即为 SigV4)。"""
    if auth == "iam":
        return None
    discovery = idp.get("DiscoveryUrl", "")
    if not discovery or "placeholder.invalid" in discovery:
        die("IdP 的 issuer 还是占位值,先跑 ./scripts/deploy.sh 回填")
    return {
        "customJWTAuthorizer": {
            "discoveryUrl": discovery,
            "allowedAudience": [project],
            "allowedClients": naming.all_client_ids(project),
        }
    }


def build_create_params(
    *,
    name: str,
    image: str,
    role_arn: str,
    environment: dict[str, str],
    authorizer: dict[str, Any] | None,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "agentRuntimeName": name,
        "description": "agentcore-cn demo agent (after-sales assistant)",
        "agentRuntimeArtifact": {"containerConfiguration": {"containerUri": image}},
        "roleArn": role_arn,
        # PUBLIC:容器需要出网访问 api.deepseek.com。
        # 中国区只支持 MicroVM,所以不传 capacityProviderConfiguration。
        "networkConfiguration": {"networkMode": "PUBLIC"},
        # 容器暴露的是 /invocations + /ping,所以是 HTTP 而不是 MCP
        "protocolConfiguration": {"serverProtocol": "HTTP"},
        "environmentVariables": environment,
    }
    if authorizer:
        params["authorizerConfiguration"] = authorizer
    return params


# ---------------------------------------------------------------------------
# 控制面操作
# ---------------------------------------------------------------------------


def find_runtime(control, name: str) -> dict[str, Any] | None:
    token = None
    while True:
        kwargs: dict[str, Any] = {"maxResults": 50}
        if token:
            kwargs["nextToken"] = token
        resp = control.list_agent_runtimes(**kwargs)
        for item in resp.get("agentRuntimes", []) or resp.get("items", []):
            if item.get("agentRuntimeName") == name:
                return item
        token = resp.get("nextToken")
        if not token:
            return None


def wait_ready(fetch, label: str, timeout: int = 600) -> dict[str, Any]:
    deadline = time.time() + timeout
    last: dict[str, Any] = {}
    while time.time() < deadline:
        last = fetch()
        status = last.get("status", "")
        if status in READY_STATES:
            log(f"{label} 已就绪")
            return last
        if status in FAILED_STATES:
            reasons = last.get("statusReasons") or last.get("failureReason") or "(无原因)"
            die(f"{label} 进入 {status}:{reasons}")
        print(f"    {label} status={status},等待中…")
        time.sleep(10)
    die(f"{label} 等待超时,最后状态 {last.get('status')}")
    return last


def ensure_runtime(control, params: dict[str, Any]) -> dict[str, Any]:
    name = params["agentRuntimeName"]
    existing = find_runtime(control, name)
    if existing is None:
        log(f"创建 Runtime {name}")
        return control.create_agent_runtime(**params)

    runtime_id = existing["agentRuntimeId"]
    log(f"Runtime {name} 已存在({runtime_id}),更新为新版本")
    update = {k: v for k, v in params.items() if k != "agentRuntimeName"}
    update["agentRuntimeId"] = runtime_id
    return control.update_agent_runtime(**update)


def ensure_stable_endpoint(
    control, runtime_id: str, version: str, name: str = STABLE_ENDPOINT
) -> dict[str, Any]:
    """命名端点,用来把一部分流量钉在某个版本上。"""
    try:
        resp = control.create_agent_runtime_endpoint(
            agentRuntimeId=runtime_id,
            name=name,
            agentRuntimeVersion=version,
            description="Pinned endpoint for canary / rollback",
        )
        log(f"已创建端点 {name} -> 版本 {version}")
        return resp
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code not in ("ConflictException", "ValidationException"):
            raise
        log(f"端点 {name} 已存在,指向版本 {version}")
        return control.update_agent_runtime_endpoint(
            agentRuntimeId=runtime_id, endpointName=name, agentRuntimeVersion=version
        )


# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------


def cmd_show(control, name: str) -> int:
    runtime = find_runtime(control, name)
    if not runtime:
        log(f"Runtime {name} 不存在")
        return 0
    runtime_id = runtime["agentRuntimeId"]
    detail = control.get_agent_runtime(agentRuntimeId=runtime_id)
    detail.pop("ResponseMetadata", None)
    print(json.dumps(detail, indent=2, default=str, ensure_ascii=False))

    print("\n版本:")
    for v in control.list_agent_runtime_versions(
        agentRuntimeId=runtime_id, maxResults=50
    ).get("agentRuntimes", []):
        print(f"  {v.get('agentRuntimeVersion')}  {v.get('status')}  {v.get('updatedAt')}")

    print("\n端点:")
    for e in control.list_agent_runtime_endpoints(
        agentRuntimeId=runtime_id, maxResults=50
    ).get("runtimeEndpoints", []):
        print(
            f"  {e.get('name'):12} -> 版本 {e.get('targetVersion') or e.get('liveVersion')}"
            f"  {e.get('status')}"
        )
    return 0


def cmd_promote(control, name: str, version: str) -> int:
    runtime = find_runtime(control, name)
    if not runtime:
        die(f"Runtime {name} 不存在")
    ensure_stable_endpoint(control, runtime["agentRuntimeId"], version)
    log(f"端点 {STABLE_ENDPOINT} 现在指向版本 {version}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default=os.environ.get("PROJECT", "agentcore-cn"))
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "cn-northwest-1"))
    parser.add_argument("--profile", default=os.environ.get("AWS_PROFILE") or None)
    parser.add_argument("--image", help="ECR 镜像 URI。不传则用仓库的 :latest")
    parser.add_argument(
        "--auth", choices=("jwt", "iam"), default="jwt",
        help="入向鉴权。jwt=自建 IdP 的 CUSTOM_JWT(默认);iam=SigV4",
    )
    parser.add_argument("--show", action="store_true", help="打印版本与端点")
    parser.add_argument("--promote", metavar="VERSION", help="把 stable 端点指向某版本")
    args = parser.parse_args()

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    partition = session.client("sts").get_caller_identity()["Arn"].split(":")[1]
    if partition != "aws-cn":
        die(f"当前凭证在分区 {partition},不是 aws-cn")

    control = session.client("bedrock-agentcore-control")
    runtime_name = f"{args.project}_agent".replace("-", "_")

    if args.show:
        return cmd_show(control, runtime_name)
    if args.promote:
        return cmd_promote(control, runtime_name, args.promote)

    cfn = session.client("cloudformation")
    foundation = stack_outputs(cfn, f"{args.project}-foundation")
    idp = stack_outputs(cfn, f"{args.project}-auth-idp")

    image = args.image or f"{foundation['AgentRepositoryUri']}:latest"

    # Gateway / 物流站点是可选的,取不到就留空让 Agent 优雅降级
    extra: dict[str, str] = {}
    try:
        extra["LOGISTICS_URL"] = stack_outputs(
            cfn, f"{args.project}-logistics-web"
        ).get("LogisticsUrl", "")
    except SystemExit:
        warn("物流页栈不存在,Browser 工具会被跳过")
    gateway_url = os.environ.get("GATEWAY_URL", "").strip()
    if gateway_url:
        extra["GATEWAY_URL"] = gateway_url
    else:
        warn("GATEWAY_URL 未设置(环境变量),业务工具会被跳过。"
             "先跑 scripts/create_gateway.py 拿到它再重跑本脚本。")

    params = build_create_params(
        name=runtime_name,
        image=image,
        role_arn=foundation["RuntimeExecutionRoleArn"],
        environment=build_environment(args.project, args.region, foundation, extra),
        authorizer=build_authorizer(args.auth, args.project, idp),
    )

    log(f"镜像 {image}")
    log(f"入向鉴权 {'CUSTOM_JWT(自建 IdP)' if args.auth == 'jwt' else 'AWS_IAM(SigV4)'}")

    resp = ensure_runtime(control, params)
    runtime_id = resp["agentRuntimeId"]
    runtime_arn = resp["agentRuntimeArn"]
    version = resp["agentRuntimeVersion"]

    wait_ready(
        lambda: control.get_agent_runtime(agentRuntimeId=runtime_id),
        f"Runtime {runtime_id}",
    )
    ensure_stable_endpoint(control, runtime_id, version)

    print()
    log("Runtime 就绪:")
    print(f"\n  AGENT_RUNTIME_ARN={runtime_arn}")
    print(f"  版本 {version} / 端点 DEFAULT(跟最新)+ {STABLE_ENDPOINT}(钉在 {version})\n")
    log("调用试试:")
    print(f"  python scripts/invoke.py --arn {runtime_arn} --prompt 'ORD-1024 到哪了'")
    print(f"  python scripts/invoke.py --arn {runtime_arn} --selftest\n")
    if args.auth == "iam":
        warn(
            "用 SigV4 入向时,invoke.py 会带上 runtimeUserId —— "
            "没有它容器里拿不到 workload access token,Identity 出向会失败。"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
