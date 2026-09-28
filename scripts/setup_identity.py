#!/usr/bin/env python3
"""配置 AgentCore Identity 的出向凭证(outbound credentials)。

建两个 credential provider:

  1. API Key provider —— 存 DeepSeek 官方 API 的 key。
     Agent 运行时用 requires_api_key(provider_name=...) 现取,
     镜像里、Runtime 配置里、CloudFormation 里都不会出现明文。

  2. OAuth2 provider(CustomOauth2)—— 指向自建 IdP 的 client_credentials。
     Agent 用它换 token 去调 Gateway(Gateway 入向是 CUSTOM_JWT)。
     中国区内置的 GitHub/Google/Slack 等 vendor 全部不可用,
     所以只能走 CustomOauth2 —— 这也正是自建 IdP 的价值所在。

密钥处理:
  * 两个 provider 都用 apiKeySecretSource/clientSecretSource = MANAGED,
    由 AgentCore 自己托管密文,我们不额外建 Secrets Manager 条目。
  * 脚本全程不打印密钥,连长度都不打印。

用法:
    export $(grep -v '^#' .env | xargs)
    python scripts/setup_identity.py                   # 创建或更新
    python scripts/setup_identity.py --show            # 只看现状
    python scripts/setup_identity.py --delete          # 删掉两个 provider
    python scripts/setup_identity.py --skip-oauth      # 只配 DeepSeek key
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys

import boto3
from botocore.exceptions import ClientError

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import naming  # noqa: E402

# 自建 IdP 同时支持 client_secret_basic 和 client_secret_post。
# 选 BASIC:secret 走 Authorization 头,不进 body,日志里更不容易漏。
CLIENT_AUTH_METHOD = "CLIENT_SECRET_BASIC"

# 中国区没有内置 OAuth vendor,只能用 CustomOauth2
OAUTH_VENDOR = "CustomOauth2"


def log(msg: str) -> None:
    print(f"\033[1;34m==>\033[0m {msg}")


def warn(msg: str) -> None:
    print(f"\033[1;33m[!]\033[0m {msg}", file=sys.stderr)


def die(msg: str) -> None:
    print(f"\033[1;31m[x]\033[0m {msg}", file=sys.stderr)
    raise SystemExit(1)


def _error_code(exc: ClientError) -> str:
    return exc.response.get("Error", {}).get("Code", "")


# ---------------------------------------------------------------------------
# 输入
# ---------------------------------------------------------------------------


def _secret_from(env_name: str, prompt: str) -> str:
    """环境变量优先,否则交互式读取(不回显)。绝不打印取到的值。"""
    value = os.environ.get(env_name, "").strip()
    if value:
        return value
    if not sys.stdin.isatty():
        die(f"{env_name} 未设置,且当前不是交互式终端")
    value = getpass.getpass(f"{prompt}: ").strip()
    if not value:
        die(f"{prompt} 不能为空")
    return value


def stack_outputs(cfn, stack_name: str) -> dict[str, str]:
    try:
        stacks = cfn.describe_stacks(StackName=stack_name)["Stacks"]
    except ClientError as exc:
        if "does not exist" in str(exc):
            die(f"栈 {stack_name} 不存在,先跑 scripts/deploy.sh")
        raise
    return {o["OutputKey"]: o["OutputValue"] for o in stacks[0].get("Outputs", [])}


# ---------------------------------------------------------------------------
# API Key provider
# ---------------------------------------------------------------------------


def ensure_api_key_provider(control, *, name: str, api_key: str) -> dict:
    params = {"name": name, "apiKey": api_key, "apiKeySecretSource": "MANAGED"}
    try:
        resp = control.create_api_key_credential_provider(**params)
        log(f"已创建 API Key provider {name}")
        return resp
    except ClientError as exc:
        if _error_code(exc) not in ("ConflictException", "ValidationException"):
            raise
        # 已存在 -> 改成更新,顺便完成密钥轮换
        log(f"API Key provider {name} 已存在,更新密钥")
        return control.update_api_key_credential_provider(**params)


# ---------------------------------------------------------------------------
# OAuth2 provider
# ---------------------------------------------------------------------------


def build_oauth_config(
    *,
    discovery_url: str,
    client_id: str,
    client_secret: str,
    issuer: str = "",
    use_metadata: bool = False,
) -> dict:
    """构造 customOauth2ProviderConfig。

    oauthDiscovery 有两种写法,二选一:
      discoveryUrl               让 AgentCore 自己去拉 .well-known 文档(默认)
      authorizationServerMetadata 直接把 issuer / 两个端点写死

    默认用 discoveryUrl,因为它顺带验证了我们自建 IdP 的 discovery 文档没写错。
    但如果 AgentCore 拉不到那个 URL(网络策略、或它对文档字段有额外要求),
    用 --oauth-metadata 切到显式写法绕过。
    """
    if use_metadata:
        if not issuer:
            die("用 --oauth-metadata 时必须能拿到 issuer")
        discovery: dict = {
            "authorizationServerMetadata": {
                "issuer": issuer,
                # client_credentials 流用不到授权端点,但这是必填字段
                "authorizationEndpoint": f"{issuer}/oauth2/authorize",
                "tokenEndpoint": f"{issuer}/oauth2/token",
                "responseTypes": ["token"],
                "tokenEndpointAuthMethods": ["client_secret_basic", "client_secret_post"],
            }
        }
    else:
        discovery = {"discoveryUrl": discovery_url}

    return {
        "customOauth2ProviderConfig": {
            "oauthDiscovery": discovery,
            "clientId": client_id,
            "clientSecret": client_secret,
            "clientSecretSource": "MANAGED",
            "clientAuthenticationMethod": CLIENT_AUTH_METHOD,
        }
    }


def ensure_oauth_provider(control, *, name: str, config: dict) -> dict:
    params = {
        "name": name,
        "credentialProviderVendor": OAUTH_VENDOR,
        "oauth2ProviderConfigInput": config,
    }
    try:
        resp = control.create_oauth2_credential_provider(**params)
        log(f"已创建 OAuth2 provider {name}")
        return resp
    except ClientError as exc:
        if _error_code(exc) not in ("ConflictException", "ValidationException"):
            raise
        log(f"OAuth2 provider {name} 已存在,更新配置")
        return control.update_oauth2_credential_provider(**params)


# ---------------------------------------------------------------------------
# 展示 / 删除
# ---------------------------------------------------------------------------


def show(control, *, api_key_name: str, oauth_name: str) -> None:
    for label, fetch in (
        (f"API Key provider {api_key_name}",
         lambda: control.get_api_key_credential_provider(name=api_key_name)),
        (f"OAuth2 provider {oauth_name}",
         lambda: control.get_oauth2_credential_provider(name=oauth_name)),
    ):
        try:
            detail = fetch()
        except ClientError as exc:
            if _error_code(exc) == "ResourceNotFoundException":
                warn(f"{label} 不存在")
                continue
            raise
        detail.pop("ResponseMetadata", None)
        log(label)
        # 响应里只有密文 ARN,没有明文,可以安全打印
        print(json.dumps(detail, indent=2, default=str, ensure_ascii=False))


def delete(control, *, api_key_name: str, oauth_name: str) -> None:
    for label, remove in (
        (f"API Key provider {api_key_name}",
         lambda: control.delete_api_key_credential_provider(name=api_key_name)),
        (f"OAuth2 provider {oauth_name}",
         lambda: control.delete_oauth2_credential_provider(name=oauth_name)),
    ):
        try:
            remove()
            log(f"已删除 {label}")
        except ClientError as exc:
            if _error_code(exc) == "ResourceNotFoundException":
                warn(f"{label} 本来就不存在")
                continue
            raise


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default=os.environ.get("PROJECT", "agentcore-cn"))
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "cn-northwest-1"))
    parser.add_argument("--profile", default=os.environ.get("AWS_PROFILE") or None)
    parser.add_argument("--show", action="store_true", help="只打印现状")
    parser.add_argument("--delete", action="store_true", help="删掉两个 provider")
    parser.add_argument("--skip-oauth", action="store_true", help="只配 DeepSeek API Key")
    parser.add_argument(
        "--oauth-metadata",
        action="store_true",
        help="OAuth2 用显式 authorizationServerMetadata 而不是 discoveryUrl",
    )
    args = parser.parse_args()

    api_key_name = os.environ.get("DEEPSEEK_API_KEY_PROVIDER", f"{args.project}-deepseek")
    oauth_name = os.environ.get("GATEWAY_OAUTH_PROVIDER", f"{args.project}-gateway-oauth")

    session = boto3.Session(profile_name=args.profile, region_name=args.region)

    identity = session.client("sts").get_caller_identity()
    partition = identity["Arn"].split(":")[1]
    if partition != "aws-cn":
        die(f"当前凭证在分区 {partition},不是 aws-cn。本项目只针对中国区。")
    log(f"账号 {identity['Account']} / 区域 {args.region}")

    control = session.client("bedrock-agentcore-control")

    if args.show:
        show(control, api_key_name=api_key_name, oauth_name=oauth_name)
        return 0
    if args.delete:
        delete(control, api_key_name=api_key_name, oauth_name=oauth_name)
        return 0

    # ---- DeepSeek API Key ----
    deepseek_key = _secret_from("DEEPSEEK_API_KEY", "DeepSeek API Key")
    api_resp = ensure_api_key_provider(control, name=api_key_name, api_key=deepseek_key)
    # 立刻从内存里丢掉,减少意外打印的机会
    del deepseek_key

    # ---- 自建 IdP 的 OAuth2 ----
    oauth_resp = None
    if not args.skip_oauth:
        cfn = session.client("cloudformation")
        idp = stack_outputs(cfn, f"{args.project}-auth-idp")
        discovery_url = idp["DiscoveryUrl"]
        issuer = idp["IssuerUrl"]
        if "placeholder.invalid" in discovery_url:
            die("IdP 的 issuer 还是占位值,先跑 scripts/deploy.sh 回填")

        # 用 seed_auth.py 建的那个 client_credentials 专用客户端
        client_id = (
            os.environ.get("GATEWAY_CLIENT_ID", "").strip()
            or naming.m2m_client_id(args.project)
        )
        client_secret = _secret_from(
            "GATEWAY_CLIENT_SECRET", f"客户端 {client_id} 的 secret"
        )
        oauth_resp = ensure_oauth_provider(
            control,
            name=oauth_name,
            config=build_oauth_config(
                discovery_url=discovery_url,
                client_id=client_id,
                client_secret=client_secret,
                issuer=issuer,
                use_metadata=args.oauth_metadata,
            ),
        )
        del client_secret

    # ---- 输出 ----
    print()
    log("Identity 出向凭证已配置。Runtime 需要这两个环境变量:")
    print(f"\n  DEEPSEEK_API_KEY_PROVIDER={api_key_name}")
    print(f"  GATEWAY_OAUTH_PROVIDER={oauth_name}\n")
    log("Runtime 的执行角色需要(00-foundation.yaml 已经给了):")
    print("  bedrock-agentcore:GetResourceApiKey")
    print("  bedrock-agentcore:GetResourceOauth2Token")
    print("  bedrock-agentcore:GetWorkloadAccessToken*")
    print()
    log("凭证密文 ARN(明文只在 AgentCore 托管的 secret 里):")
    for label, resp, key in (
        ("DeepSeek key", api_resp, "apiKeySecretArn"),
        ("IdP client secret", oauth_resp, "clientSecretArn"),
    ):
        if not resp:
            continue
        arn = resp.get(key)
        # 这个字段是个 structure,里面是 {"secretArn": "..."}
        if isinstance(arn, dict):
            arn = arn.get("secretArn", arn)
        print(f"  {label:<20} {arn}")
    print()
    warn(
        "重要:AWS_IAM(SigV4)入向调用 Runtime 时,必须带上\n"
        "    X-Amzn-Bedrock-AgentCore-Runtime-User-Id\n"
        "    否则容器里拿不到 workload access token,Identity 出向会失败。\n"
        "    用 CUSTOM_JWT 入向则不需要 —— WAT 从 JWT 推导。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
