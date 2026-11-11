"""自定义字段：落库自动加密、读出自动解密。"""
from __future__ import annotations

import logging

from django.db import models

from . import crypto

logger = logging.getLogger(__name__)


class UndecryptableValue(str):
    """解密失败时的占位值。

    - 对外表现为空字符串（`str(v) == ""`），业务代码不会拿到密文；
    - 但内部保留了原始密文 `raw`，写回时原样落库。

    没有这个类型的话，"解密失败 → 返回空串 → 之后任何一次 save() 把列覆盖成空"
    会直接造成**密码永久丢失**：密钥配错一次，数据就没了。
    """

    __slots__ = ("raw",)

    def __new__(cls, raw: str):
        obj = super().__new__(cls, "")
        obj.raw = raw
        return obj


class EncryptedTextField(models.TextField):
    """透明加密的文本字段。

    写入时（get_prep_value）加密，读取时（from_db_value）解密。
    因此 Python 侧 `instance.password_ciphertext` 拿到的是明文，而数据库里
    只有密文——避免了"业务代码忘了加密"这类事故。
    """

    description = "Fernet 加密存储的文本"

    def get_prep_value(self, value):
        # 必须在"空值判断"之前处理：UndecryptableValue 的字符串值就是 ""，
        # 一旦被当成空值处理，原密文就会被空串覆盖。
        if isinstance(value, UndecryptableValue):
            return value.raw
        value = super().get_prep_value(value)
        if value in (None, ""):
            return value
        # 约定：Python 侧永远是明文，加密只发生在写库这一刻。
        # 这样模型方法、序列化器、任务都不需要关心密文格式。
        return crypto.encrypt(value)

    def from_db_value(self, value, expression, connection):
        if value in (None, ""):
            return value
        try:
            return crypto.decrypt(value)
        except crypto.DecryptionError:
            # 不抛异常：单条坏数据不应让整个列表接口 500。
            # 同时把问题显式记录到日志，运维可据此排查密钥配置。
            logger.error(
                "字段解密失败 field=%s table=%s 密文前缀=%r",
                self.name,
                self.model._meta.db_table if self.model else "?",
                (value or "")[:16],
            )
            return UndecryptableValue(value)
