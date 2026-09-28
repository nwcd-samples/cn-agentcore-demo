"""IdP 测试的公共装置。

handler 在 import 时就读环境变量并建 boto3 客户端,所以 env 必须在 import 之前设好,
客户端则在 fixture 里换成内存假实现。整套测试不需要任何 AWS 凭证。
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import ClientError

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src" / "lambdas" / "idp"))
sys.path.insert(0, str(REPO_ROOT / "src" / "lambdas" / "tools"))
sys.path.insert(0, str(REPO_ROOT / "src" / "lambdas" / "logistics"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

TEST_ISSUER = "https://idp.example.test"

os.environ.setdefault("AUTH_TABLE", "test-auth-table")
os.environ.setdefault("KMS_KEY_ID", "arn:aws-cn:kms:cn-northwest-1:111122223333:key/test")
os.environ.setdefault("ISSUER", TEST_ISSUER)
os.environ.setdefault("DEFAULT_AUDIENCE", "agentcore-cn")
# 让 boto3 在 import 期建客户端时不去找真实凭证/区域
os.environ.setdefault("AWS_DEFAULT_REGION", "cn-northwest-1")
os.environ.setdefault("AWS_REGION", "cn-northwest-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
# Agent 侧配置:给个假 key,避免测试里去 Identity 换凭证
os.environ.setdefault("PROJECT", "agentcore-cn")
os.environ.setdefault("DEEPSEEK_API_KEY", "sk-test-not-a-real-key")
os.environ.setdefault("MEMORY_TABLE", "agentcore-cn-memory")
os.environ.setdefault("BUSINESS_TABLE", "agentcore-cn-business")

import rsa_stub  # noqa: E402


class FakeDynamoDB:
    """够用的 DynamoDB 假实现:只支持 handler 用到的三个操作。"""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}

    @staticmethod
    def _key_of(key: dict[str, Any]) -> tuple[str, str]:
        return key["PK"]["S"], key["SK"]["S"]

    def get_item(
        self,
        *,
        TableName: str,
        Key: dict,
        ConsistentRead: bool = False,
        ProjectionExpression: str | None = None,
    ) -> dict:
        item = self.items.get(self._key_of(Key))
        if not item:
            return {}
        if ProjectionExpression:
            keep = {f.strip() for f in ProjectionExpression.split(",")}
            return {"Item": {k: v for k, v in item.items() if k in keep}}
        return {"Item": dict(item)}

    # 项目里用到的两种条件表达式,都是"这条键不存在时才写"
    _SUPPORTED_CONDITIONS = {"attribute_not_exists(SK)", "attribute_not_exists(PK)"}

    def put_item(self, *, TableName: str, Item: dict, ConditionExpression: str = "") -> dict:
        key = (Item["PK"]["S"], Item["SK"]["S"])
        if ConditionExpression:
            if ConditionExpression.strip() not in self._SUPPORTED_CONDITIONS:
                raise NotImplementedError(
                    f"假 DynamoDB 不支持条件表达式 {ConditionExpression!r}"
                )
            if key in self.items:
                raise ClientError(
                    {"Error": {"Code": "ConditionalCheckFailedException", "Message": "exists"}},
                    "PutItem",
                )
        self.items[key] = dict(Item)
        return {}

    def delete_item(self, *, TableName: str, Key: dict, ReturnValues: str = "NONE") -> dict:
        removed = self.items.pop(self._key_of(Key), None)
        if ReturnValues == "ALL_OLD" and removed:
            return {"Attributes": removed}
        return {}


class FakeKMS:
    """用纯 Python RSA 冒充 KMS 的 GetPublicKey / Sign / Verify。"""

    def __init__(self, key: rsa_stub.RsaKey) -> None:
        self.key = key
        self.sign_calls = 0
        self.get_public_key_calls = 0
        self.verify_calls = 0

    def get_public_key(self, *, KeyId: str) -> dict:
        self.get_public_key_calls += 1
        return {"PublicKey": rsa_stub.public_key_to_spki_der(self.key)}

    def sign(self, *, KeyId: str, Message: bytes, MessageType: str, SigningAlgorithm: str) -> dict:
        # 断言 handler 用的是 AgentCore 侧能验的 RS256 组合
        assert MessageType == "RAW"
        assert SigningAlgorithm == "RSASSA_PKCS1_V1_5_SHA_256"
        self.sign_calls += 1
        return {"Signature": rsa_stub.sign_pkcs1v15_sha256(self.key, Message)}

    def verify(
        self,
        *,
        KeyId: str,
        Message: bytes,
        MessageType: str,
        Signature: bytes,
        SigningAlgorithm: str,
    ) -> dict:
        assert MessageType == "RAW"
        assert SigningAlgorithm == "RSASSA_PKCS1_V1_5_SHA_256"
        self.verify_calls += 1
        valid = rsa_stub.verify_pkcs1v15_sha256(
            self.key.n, self.key.e, Message, Signature
        )
        if not valid:
            # 真实 KMS 在签名不匹配时抛异常而不是返回 False
            raise ClientError(
                {
                    "Error": {
                        "Code": "KMSInvalidSignatureException",
                        "Message": "signature does not match",
                    }
                },
                "Verify",
            )
        return {"SignatureValid": True}


@pytest.fixture(scope="session")
def rsa_key() -> rsa_stub.RsaKey:
    # 1024 位:测的是数学与编码,不是密钥强度,小一点跑得快
    return rsa_stub.generate_key(1024)


@pytest.fixture
def idp(monkeypatch: pytest.MonkeyPatch, rsa_key: rsa_stub.RsaKey):
    """返回 (IdP handler 模块, 假 DDB, 假 KMS)。"""
    import idp_handler as handler

    fake_ddb = FakeDynamoDB()
    fake_kms = FakeKMS(rsa_key)
    monkeypatch.setattr(handler, "_ddb", fake_ddb)
    monkeypatch.setattr(handler, "_kms", fake_kms)
    # 公钥缓存是模块级的,每个用例要清掉
    monkeypatch.setattr(handler, "_jwks_cache", None)
    # 真实轮数在测试里太慢
    monkeypatch.setattr(handler, "PBKDF2_ROUNDS", 1000)
    return handler, fake_ddb, fake_kms


# ---------------------------------------------------------------------------
# Agent 侧用的 DynamoDB 假实现:比 IdP 那个多一个 query
# ---------------------------------------------------------------------------


class QueryableFakeDynamoDB(FakeDynamoDB):
    """支持 MemoryLite 和业务工具用到的 query 形态。

    只实现真正用到的子集:
      KeyConditionExpression = "<PK属性> = :pk AND begins_with(<SK属性>, :prefix)"
      IndexName(GSI1)/ ScanIndexForward / Limit / ProjectionExpression
    别的写法直接抛错,避免测试悄悄依赖没实现的行为。
    """

    # 主表与 GSI1 各自的 (分区键, 排序键) 属性名
    _KEY_SCHEMA = {None: ("PK", "SK"), "GSI1": ("GSI1PK", "GSI1SK")}

    # 设成正整数可强制分页,用来验证调用方真的会翻页
    page_size: int | None = None

    _COND_RE = re.compile(
        r"^\s*(?P<pk>\w+)\s*=\s*:pk\s+AND\s+begins_with\(\s*(?P<sk>\w+)\s*,\s*:prefix\s*\)\s*$",
        re.IGNORECASE,
    )

    def query(
        self,
        *,
        TableName: str,
        KeyConditionExpression: str,
        ExpressionAttributeValues: dict,
        IndexName: str | None = None,
        ScanIndexForward: bool = True,
        Limit: int | None = None,
        ProjectionExpression: str | None = None,
        ConsistentRead: bool = False,
        Select: str | None = None,
        ExclusiveStartKey: dict | None = None,
    ) -> dict:
        match = self._COND_RE.match(KeyConditionExpression)
        if not match:
            raise NotImplementedError(
                "假 DynamoDB 只支持 '<pk> = :pk AND begins_with(<sk>, :prefix)',"
                f"收到 {KeyConditionExpression!r}"
            )
        expected = self._KEY_SCHEMA.get(IndexName)
        if expected is None:
            raise NotImplementedError(f"假 DynamoDB 不认识索引 {IndexName!r}")
        pk_attr, sk_attr = match.group("pk"), match.group("sk")
        if (pk_attr, sk_attr) != expected:
            raise AssertionError(
                f"索引 {IndexName or '主表'} 的键应该是 {expected},"
                f"查询却用了 ({pk_attr}, {sk_attr})"
            )

        pk = ExpressionAttributeValues[":pk"]["S"]
        prefix = ExpressionAttributeValues[":prefix"]["S"]

        matched = [
            item
            for item in self.items.values()
            if item.get(pk_attr, {}).get("S") == pk
            and item.get(sk_attr, {}).get("S", "").startswith(prefix)
        ]
        # DynamoDB 按排序键排序;项目里的排序键都是零填充的,字典序即数值序
        matched.sort(key=lambda it: it[sk_attr]["S"], reverse=not ScanIndexForward)

        # Select=COUNT 只返回条数,不返回 Items
        if Select == "COUNT":
            return {"Count": len(matched), "ScannedCount": len(matched)}

        if ExclusiveStartKey is not None:
            # 从这个键之后继续。真实 DynamoDB 用它做分页游标。
            start_sk = ExclusiveStartKey[sk_attr]["S"]
            matched = [i for i in matched if i[sk_attr]["S"] > start_sk]

        page = matched[:Limit] if Limit is not None else matched
        if ProjectionExpression:
            keep = {f.strip() for f in ProjectionExpression.split(",")}
            page = [{k: v for k, v in item.items() if k in keep} for item in page]

        result: dict = {"Items": [dict(item) for item in page], "Count": len(page)}
        # 还有剩余时给出游标,让调用方必须正确翻页
        if self.page_size and len(page) > self.page_size:
            page = page[: self.page_size]
            result["Items"] = [dict(i) for i in page]
            result["Count"] = len(page)
            result["LastEvaluatedKey"] = {
                pk_attr: {"S": pk},
                sk_attr: page[-1][sk_attr],
            }
        return result


@pytest.fixture
def fake_ddb_factory():
    return QueryableFakeDynamoDB


# ---------------------------------------------------------------------------
# 两个 Lambda 的入口文件刻意取了不同名字(idp_handler / tools_handler)。
# 早期两个都叫 handler.py,结果 scripts/create_gateway.py 把 tools 的那个以
# "handler" 名字装进 sys.modules,覆盖了 IdP 的同名模块,导致 IdP 的 55 个
# 用例集体报 AttributeError。文件名唯一是最省事的根治办法。
# ---------------------------------------------------------------------------

os.environ.setdefault("BUSINESS_TABLE", "agentcore-cn-business")
