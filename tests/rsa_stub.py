"""测试专用的纯 Python RSA 实现。

存在的理由:要验证 IdP 手拼的 JWT 和自研的 DER 解析器真的对,就需要一把真 RSA 密钥。
但 `cryptography` 在 Python 3.14 / x86_64 macOS 上没有 wheel,源码构建要 Rust。
与其为了跑测试拉一条编译链,不如把 RSA 的那点数学写出来。

安全声明:这里的实现只求正确,不防侧信道,**只能用于测试**。生产签名走 KMS。
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass

# SHA-256 的 PKCS#1 v1.5 DigestInfo 前缀(RFC 8017 附录 B.1)
_SHA256_DIGEST_INFO_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")


def _is_probable_prime(n: int, rounds: int = 24) -> bool:
    """Miller-Rabin。"""
    if n < 2:
        return False
    for p in (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37):
        if n == p:
            return True
        if n % p == 0:
            return False
    d = n - 1
    s = 0
    while d % 2 == 0:
        d //= 2
        s += 1
    for _ in range(rounds):
        a = secrets.randbelow(n - 3) + 2
        x = pow(a, d, n)
        if x in (1, n - 1):
            continue
        for _ in range(s - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def _gen_prime(bits: int) -> int:
    while True:
        candidate = secrets.randbits(bits) | (1 << (bits - 1)) | 1
        if _is_probable_prime(candidate):
            return candidate


@dataclass(frozen=True)
class RsaKey:
    n: int
    e: int
    d: int

    @property
    def key_size_bytes(self) -> int:
        return (self.n.bit_length() + 7) // 8


def generate_key(bits: int = 1024) -> RsaKey:
    """默认 1024 位:测试只关心数学正确性,小一点跑得快。"""
    e = 65537
    while True:
        p = _gen_prime(bits // 2)
        q = _gen_prime(bits // 2)
        if p == q:
            continue
        phi = (p - 1) * (q - 1)
        if phi % e == 0:
            continue
        n = p * q
        if n.bit_length() != bits:
            continue
        return RsaKey(n=n, e=e, d=pow(e, -1, phi))


# ---------------------------------------------------------------------------
# PKCS#1 v1.5 签名 / 验签
# ---------------------------------------------------------------------------


def _pkcs1v15_encode(message: bytes, key_size: int) -> bytes:
    digest_info = _SHA256_DIGEST_INFO_PREFIX + hashlib.sha256(message).digest()
    padding_len = key_size - len(digest_info) - 3
    if padding_len < 8:
        raise ValueError("RSA key too small for PKCS#1 v1.5 SHA-256")
    return b"\x00\x01" + b"\xff" * padding_len + b"\x00" + digest_info


def sign_pkcs1v15_sha256(key: RsaKey, message: bytes) -> bytes:
    """等价于 KMS 的 SigningAlgorithm=RSASSA_PKCS1_V1_5_SHA_256, MessageType=RAW。"""
    block = _pkcs1v15_encode(message, key.key_size_bytes)
    signature = pow(int.from_bytes(block, "big"), key.d, key.n)
    return signature.to_bytes(key.key_size_bytes, "big")


def verify_pkcs1v15_sha256(n: int, e: int, message: bytes, signature: bytes) -> bool:
    key_size = (n.bit_length() + 7) // 8
    if len(signature) != key_size:
        return False
    recovered = pow(int.from_bytes(signature, "big"), e, n).to_bytes(key_size, "big")
    return secrets.compare_digest(recovered, _pkcs1v15_encode(message, key_size))


# ---------------------------------------------------------------------------
# DER 编码:构造 KMS GetPublicKey 会返回的 SubjectPublicKeyInfo
# ---------------------------------------------------------------------------


def _der_len(length: int) -> bytes:
    if length < 0x80:
        return bytes([length])
    raw = length.to_bytes((length.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(raw)]) + raw


def _der_tlv(tag: int, value: bytes) -> bytes:
    return bytes([tag]) + _der_len(len(value)) + value


def _der_integer(value: int) -> bytes:
    raw = value.to_bytes((value.bit_length() + 7) // 8 or 1, "big")
    # DER INTEGER 有符号,最高位为 1 时要补 0x00
    if raw[0] & 0x80:
        raw = b"\x00" + raw
    return _der_tlv(0x02, raw)


def public_key_to_spki_der(key: RsaKey) -> bytes:
    """rsaEncryption OID 1.2.840.113549.1.1.1 + NULL 参数。"""
    rsa_public_key = _der_tlv(0x30, _der_integer(key.n) + _der_integer(key.e))
    algorithm_id = _der_tlv(
        0x30,
        _der_tlv(0x06, bytes.fromhex("2a864886f70d010101")) + _der_tlv(0x05, b""),
    )
    # BIT STRING:第一个字节是未使用位数,RSA 场景恒为 0
    bit_string = _der_tlv(0x03, b"\x00" + rsa_public_key)
    return _der_tlv(0x30, algorithm_id + bit_string)
