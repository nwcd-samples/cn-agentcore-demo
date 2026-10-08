#!/usr/bin/env python3
"""把 Demo 用户与 OAuth 客户端写入自建 IdP 的鉴权表。

默认创建三类客户端：
  * 交互式 Demo 用户客户端（password + refresh_token）
  * Agent Runtime 调 Gateway 的 M2M 客户端
  * Amazon Quick 团队级 Remote MCP 的独立 M2M 客户端

密钥只以 PBKDF2 哈希落库，明文不进入 CloudFormation。使用 --quick-only 可只
创建/轮换 Quick 客户端，不影响现有 Demo 用户和 Runtime M2M 客户端。
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

os.environ.setdefault("AUTH_TABLE", "unused-by-seed")
os.environ.setdefault("KMS_KEY_ID", "unused-by-seed")
os.environ.setdefault("ISSUER", "https://unused-by-seed.invalid")

from idp_handler import hash_password  # noqa: E402

import naming  # noqa: E402

USER_SCOPES = naming.USER_SCOPES
CLIENT_SCOPES = naming.USER_CLIENT_SCOPES
M2M_SCOPES = naming.M2M_CLIENT_SCOPES
QUICK_SCOPES = naming.QUICK_CLIENT_SCOPES


def _secret_from(env_name: str, prompt: str, *, generate: bool) -> tuple[str, bool]:
    """返回（密钥，是否现场生成）。"""
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


def _put_quick_client(ddb, table: str, project: str) -> tuple[str, str, bool]:
    client_id = naming.quick_client_id(project)
    client_secret, generated = _secret_from(
        "QUICK_CLIENT_SECRET",
        f"Amazon Quick 客户端 {client_id} 的 secret",
        generate=True,
    )
    put_client(
        ddb,
        table,
        client_id,
        client_secret,
        grant_types=["client_credentials"],
        scopes=QUICK_SCOPES,
        audience=project,
    )
    return client_id, client_secret, generated


def _client_error(exc: ClientError, table: str) -> int:
    code = exc.response.get("Error", {}).get("Code", "")
    if code == "ResourceNotFoundException":
        print(f"找不到表 {table},先部署 00-foundation.yaml", file=sys.stderr)
    else:
        print(f"写入失败:{code}", file=sys.stderr)
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default=os.environ.get("PROJECT", "agentcore-cn"))
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "cn-northwest-1"))
    parser.add_argument("--table", default=None, help="默认为 <project>-auth")
    parser.add_argument("--username", default=os.environ.get("DEMO_USERNAME", "demo-user"))
    parser.add_argument("--client-id", default=None)
    parser.add_argument(
        "--quick-only",
        action="store_true",
        help="只创建/轮换 Amazon Quick 的独立 client_credentials 客户端",
    )
    args = parser.parse_args()

    table = args.table or f"{args.project}-auth"
    audience = args.project
    try:
        naming.gateway_client_ids(args.project)
    except ValueError as exc:
        parser.error(str(exc))
    ddb = boto3.client("dynamodb", region_name=args.region)

    if args.quick_only:
        try:
            quick_id, quick_secret, generated = _put_quick_client(
                ddb, table, args.project
            )
        except ClientError as exc:
            return _client_error(exc, table)
        print(f"已写入 Amazon Quick Service-to-Service 客户端 {quick_id}")
        print("  grant_types=client_credentials")
        print(f"  scopes={' '.join(QUICK_SCOPES)}")
        if generated:
            print(f"  新生成的 QUICK_CLIENT_SECRET:{quick_secret}")
            print("  请立即保存；鉴权表中仅保存 PBKDF2 哈希。")
        else:
            print("  QUICK_CLIENT_SECRET 已从环境变量读取（不回显）。")
        return 0

    client_id = args.client_id or naming.user_client_id(args.project)

    user_password, user_generated = _secret_from(
        "DEMO_PASSWORD", f"用户 {args.username} 的密码", generate=True
    )
    client_secret, client_generated = _secret_from(
        "DEMO_CLIENT_SECRET", f"客户端 {client_id} 的 secret", generate=True
    )
    m2m_client_id = naming.m2m_client_id(args.project)
    # 保持原部署流程：Runtime M2M 每次完整 seed 都生成并打印新 secret。
    m2m_secret = secrets.token_urlsafe(24)

    try:
        put_user(
            ddb,
            table,
            args.username,
            user_password,
            actor_id=f"actor-{args.username}",
        )
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
        quick_id, quick_secret, quick_generated = _put_quick_client(
            ddb, table, args.project
        )
    except ClientError as exc:
        return _client_error(exc, table)

    print(f"已写入鉴权表 {table}:")
    print(f"  用户        {args.username}  (actor_id=actor-{args.username})")
    print(f"  用户客户端  {client_id}  grants=password,refresh_token")
    print(f"  Runtime M2M {m2m_client_id}  grants=client_credentials")
    print(f"  Quick M2M   {quick_id}  grants=client_credentials")
    print()
    if user_generated:
        print(f"  生成的用户密码:       {user_password}")
    if client_generated:
        print(f"  生成的客户端密钥:     {client_secret}")
    print(f"  Runtime M2M 客户端密钥:{m2m_secret}")
    if quick_generated:
        print(f"  Quick M2M 客户端密钥:  {quick_secret}")
    else:
        print("  Quick M2M 客户端密钥:  已从 QUICK_CLIENT_SECRET 读取（不回显）")
    print()
    print("以上新生成的明文不会再次出现，鉴权表中只有 PBKDF2 哈希。请妥善保存。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
