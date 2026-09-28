#!/usr/bin/env python3
"""把演示用户和 OAuth 客户端写进鉴权表。

密码/密钥只以 PBKDF2 哈希形式落库,明文既不进 CloudFormation 模板,
也不进 CloudFormation 事件历史 —— 这就是不用 CFN 自定义资源做 seed 的原因。

哈希函数直接从 IdP Lambda 里 import,保证两边算法和轮数永远一致,
不会出现"脚本写进去的密码 Lambda 验不过"这种问题。

用法:
    export $(grep -v '^#' .env | xargs)
    python scripts/seed_auth.py

密钥来源优先级:环境变量 -> 交互式输入(不回显)。
没给就现场生成一个强随机值并打印一次,自己存好。
"""

from __future__ import annotations

import argparse
import getpass
import os
import secrets
import sys
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src" / "lambdas" / "idp"))

# handler 在 import 时要读这几个环境变量,但 seed 只用到 hash_password,
# 所以填占位值即可,不会真的去连 KMS。
os.environ.setdefault("AUTH_TABLE", "unused-by-seed")
os.environ.setdefault("KMS_KEY_ID", "unused-by-seed")
os.environ.setdefault("ISSUER", "https://unused-by-seed.invalid")

from idp_handler import hash_password  # noqa: E402

import naming  # noqa: E402

# scope 约定集中在 naming.py,和 agent 侧请求的 scope 由测试保证一致
USER_SCOPES = naming.USER_SCOPES
CLIENT_SCOPES = naming.USER_CLIENT_SCOPES
M2M_SCOPES = naming.M2M_CLIENT_SCOPES


def _secret_from(env_name: str, prompt: str, *, generate: bool) -> tuple[str, bool]:
    """返回 (密钥, 是否是现场生成的)。"""
    value = os.environ.get(env_name, "").strip()
    if value:
        return value, False
    if sys.stdin.isatty():
        value = getpass.getpass(f"{prompt}(回车则自动生成): ").strip()
        if value:
            return value, False
    if not generate:
        raise SystemExit(f"{env_name} 未设置,且当前不是交互式终端")
    return secrets.token_urlsafe(24), True


def put_user(ddb, table: str, username: str, password: str, actor_id: str) -> None:
    ddb.put_item(
        TableName=table,
        Item={
            "PK": {"S": f"USER#{username}"},
            "SK": {"S": "PROFILE"},
            "password_hash": {"S": hash_password(password)},
            "scopes": {"SS": USER_SCOPES},
            "actor_id": {"S": actor_id},
            "disabled": {"BOOL": False},
        },
    )


def put_client(
    ddb,
    table: str,
    client_id: str,
    client_secret: str,
    grant_types: list[str],
    scopes: list[str],
    audience: str,
) -> None:
    ddb.put_item(
        TableName=table,
        Item={
            "PK": {"S": f"CLIENT#{client_id}"},
            "SK": {"S": "PROFILE"},
            "secret_hash": {"S": hash_password(client_secret)},
            "grant_types": {"SS": grant_types},
            "scopes": {"SS": scopes},
            "audience": {"S": audience},
        },
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default=os.environ.get("PROJECT", "agentcore-cn"))
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "cn-northwest-1"))
    parser.add_argument("--table", default=None, help="默认为 <project>-auth")
    parser.add_argument("--username", default=os.environ.get("DEMO_USERNAME", "demo-user"))
    # 客户端 ID 的推导集中在 scripts/naming.py,三个脚本共用,避免漂移
    parser.add_argument("--client-id", default=None)
    args = parser.parse_args()

    table = args.table or f"{args.project}-auth"
    audience = args.project
    client_id = args.client_id or naming.user_client_id(args.project)

    user_password, user_generated = _secret_from(
        "DEMO_PASSWORD", f"用户 {args.username} 的密码", generate=True
    )
    client_secret, client_generated = _secret_from(
        "DEMO_CLIENT_SECRET", f"客户端 {client_id} 的 secret", generate=True
    )
    # Gateway 出向专用的机器客户端,secret 一律自动生成
    m2m_client_id = naming.m2m_client_id(args.project)
    m2m_secret = secrets.token_urlsafe(24)

    ddb = boto3.client("dynamodb", region_name=args.region)

    try:
        put_user(ddb, table, args.username, user_password, actor_id=f"actor-{args.username}")
        put_client(
            ddb,
            table,
            client_id,
            client_secret,
            grant_types=["password", "refresh_token"],
            scopes=CLIENT_SCOPES,
            audience=audience,
        )
        put_client(
            ddb,
            table,
            m2m_client_id,
            m2m_secret,
            grant_types=["client_credentials"],
            scopes=M2M_SCOPES,
            audience=audience,
        )
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code == "ResourceNotFoundException":
            print(f"找不到表 {table},先部署 00-foundation.yaml", file=sys.stderr)
        else:
            print(f"写入失败:{code}", file=sys.stderr)
        return 1

    print(f"已写入鉴权表 {table}:")
    print(f"  用户       {args.username}  (actor_id=actor-{args.username})")
    print(f"  用户客户端 {client_id}  grants=password,refresh_token")
    print(f"  机器客户端 {m2m_client_id}  grants=client_credentials")
    print()
    # 生成的密钥只在这里出现一次,之后库里只有哈希
    if user_generated:
        print(f"  生成的用户密码:  {user_password}")
    if client_generated:
        print(f"  生成的客户端密钥:{client_secret}")
    print(f"  机器客户端密钥:  {m2m_secret}")
    print()
    print("以上明文不会再出现第二次,库里只有 PBKDF2 哈希。请自行妥善保存。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
