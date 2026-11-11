"""密码字段的加解密与随机密码生成。

- 使用 Fernet（AES-128-CBC + HMAC-SHA256）做对称加密，密文带 `enc:v1:` 前缀，
  便于将来平滑升级算法。
- 支持多密钥：`INSTANCE_ENCRYPTION_KEYS` 的第一个用于加密，其余仅用于解密，
  这样可以在不丢历史数据的前提下轮换主密钥。
"""
from __future__ import annotations

import base64
import hashlib
import logging
import secrets
from functools import lru_cache
from typing import Iterable

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

logger = logging.getLogger(__name__)

TOKEN_PREFIX = "enc:v1:"

# 去掉易混淆字符（l / I / O / 0 / 1），方便运维抄写
LOWER = "abcdefghijkmnopqrstuvwxyz"
UPPER = "ABCDEFGHJKLMNPQRSTUVWXYZ"
DIGITS = "23456789"
SYMBOLS = "!@#$%^&*-_=+?"
ALPHABET = LOWER + UPPER + DIGITS + SYMBOLS
REQUIRED_CLASSES = (LOWER, UPPER, DIGITS, SYMBOLS)


class DecryptionError(Exception):
    """密文无法解密（密钥不匹配或数据被篡改）。"""


def _normalise_key(key: str) -> bytes:
    """接受标准 Fernet key，也接受任意口令（派生为 32 字节密钥）。"""
    key = key.strip()
    if not key:
        raise ImproperlyConfigured("INSTANCE_ENCRYPTION_KEYS 中存在空密钥")
    try:
        Fernet(key.encode())
    except (ValueError, TypeError):
        digest = hashlib.sha256(key.encode("utf-8")).digest()
        return base64.urlsafe_b64encode(digest)
    return key.encode()


def _configured_keys() -> Iterable[str]:
    keys = list(getattr(settings, "INSTANCE_ENCRYPTION_KEYS", []) or [])
    if keys:
        return keys
    # 未显式配置时从 SECRET_KEY 派生，保证“装好就能跑”。
    # 生产环境必须显式配置：`manage.py check --deploy` 会把它报成安全问题
    # （见 inventory/checks.py），因为 SECRET_KEY 一旦变更，历史密文就再也解不开。
    logger.warning(
        "INSTANCE_ENCRYPTION_KEYS 未配置，正在从 SECRET_KEY 派生密钥；"
        "生产环境请显式配置，否则 SECRET_KEY 变更会导致已加密密码无法解密。"
    )
    return [settings.SECRET_KEY]


@lru_cache(maxsize=1)
def get_fernet() -> MultiFernet:
    return MultiFernet([Fernet(_normalise_key(k)) for k in _configured_keys()])


def encrypt(plaintext: str) -> str:
    """明文 -> `enc:v1:<token>`。空字符串原样返回（表示“未设置密码”）。"""
    if plaintext is None:
        raise ValueError("不能加密 None")
    if plaintext == "":
        return ""
    token = get_fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")
    return TOKEN_PREFIX + token


def decrypt(ciphertext: str) -> str:
    """`enc:v1:<token>` -> 明文。"""
    if ciphertext is None or ciphertext == "":
        return ""
    if not ciphertext.startswith(TOKEN_PREFIX):
        raise DecryptionError("密文缺少 enc:v1: 前缀，可能被明文写入")
    token = ciphertext[len(TOKEN_PREFIX) :]
    try:
        return get_fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise DecryptionError("解密失败：密钥不匹配或密文被篡改") from exc


def generate_password(length: int | None = None) -> str:
    """生成随机密码，保证四类字符各至少一个。"""
    length = length or getattr(settings, "INSTANCE_PASSWORD_LENGTH", 24)
    if length < 8:
        raise ValueError("密码长度至少 8 位")
    rng = secrets.SystemRandom()
    while True:
        candidate = "".join(rng.choice(ALPHABET) for _ in range(length))
        if all(any(ch in group for ch in candidate) for group in REQUIRED_CLASSES):
            return candidate


def reset_fernet_cache() -> None:
    """测试/密钥变更后清缓存。"""
    get_fernet.cache_clear()
