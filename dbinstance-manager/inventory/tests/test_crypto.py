"""加密与随机密码生成的单元测试。"""
from __future__ import annotations

from cryptography.fernet import Fernet
from django.db import connection
from django.test import TestCase, override_settings

from inventory import crypto
from inventory.tests.factories import make_instance


class CryptoFunctionTests(TestCase):
    def setUp(self):
        self.addCleanup(crypto.reset_fernet_cache)

    def test_roundtrip_including_cjk(self):
        token = crypto.encrypt("p@ssw0rd-中文密码")
        self.assertTrue(token.startswith(crypto.TOKEN_PREFIX))
        self.assertNotIn("p@ssw0rd", token)
        self.assertEqual(crypto.decrypt(token), "p@ssw0rd-中文密码")

    def test_same_plaintext_produces_different_ciphertext(self):
        # Fernet 每次加密使用随机 IV，因此同一明文两次加密结果不同
        self.assertNotEqual(crypto.encrypt("same"), crypto.encrypt("same"))

    def test_empty_values(self):
        self.assertEqual(crypto.encrypt(""), "")
        self.assertEqual(crypto.decrypt(""), "")

    def test_decrypt_rejects_garbage(self):
        with self.assertRaises(crypto.DecryptionError):
            crypto.decrypt("enc:v1:this-is-not-a-token")
        with self.assertRaises(crypto.DecryptionError):
            crypto.decrypt("plain-text-without-prefix")

    def test_generate_password_rules(self):
        passwords = {crypto.generate_password() for _ in range(50)}
        self.assertEqual(len(passwords), 50, "随机密码出现重复，熵不足")
        for password in passwords:
            self.assertEqual(len(password), 24)
            self.assertTrue(any(c in crypto.LOWER for c in password))
            self.assertTrue(any(c in crypto.UPPER for c in password))
            self.assertTrue(any(c in crypto.DIGITS for c in password))
            self.assertTrue(any(c in crypto.SYMBOLS for c in password))

    def test_generate_password_rejects_short_length(self):
        with self.assertRaises(ValueError):
            crypto.generate_password(6)

    def test_key_rotation_supports_old_key_as_fallback(self):
        key_old = Fernet.generate_key().decode()
        key_new = Fernet.generate_key().decode()

        with override_settings(INSTANCE_ENCRYPTION_KEYS=[key_old]):
            crypto.reset_fernet_cache()
            legacy_token = crypto.encrypt("legacy-secret")

        # 新主密钥 + 旧密钥兜底：能解旧数据，且新数据用新密钥加密
        with override_settings(INSTANCE_ENCRYPTION_KEYS=[key_new, key_old]):
            crypto.reset_fernet_cache()
            self.assertEqual(crypto.decrypt(legacy_token), "legacy-secret")
            self.assertEqual(crypto.decrypt(crypto.encrypt("legacy-secret")), "legacy-secret")

        # 只留新密钥时，旧数据解不开（说明确实换了密钥）
        with override_settings(INSTANCE_ENCRYPTION_KEYS=[key_new]):
            crypto.reset_fernet_cache()
            with self.assertRaises(crypto.DecryptionError):
                crypto.decrypt(legacy_token)


class EncryptedFieldStorageTests(TestCase):
    """确认“Python 侧明文、数据库侧密文”。"""

    def test_database_stores_ciphertext_only(self):
        instance = make_instance(password="Plain#Text123")

        self.assertEqual(instance.password, "Plain#Text123")

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT password_ciphertext FROM inventory_dbinstance WHERE id = %s", [instance.pk]
            )
            raw = cursor.fetchone()[0]
        self.assertTrue(raw.startswith(crypto.TOKEN_PREFIX))
        self.assertNotIn("Plain#Text123", raw)

        instance.refresh_from_db()
        self.assertEqual(instance.password, "Plain#Text123")

    def test_password_history_is_encrypted_too(self):
        instance = make_instance(password="History#Secret1")
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT password_ciphertext FROM inventory_instancepasswordhistory WHERE instance_id = %s",
                [instance.pk],
            )
            rows = [row[0] for row in cursor.fetchall()]
        self.assertEqual(len(rows), 1)
        for raw in rows:
            self.assertTrue(raw.startswith(crypto.TOKEN_PREFIX))
            self.assertNotIn("History#Secret1", raw)

    def test_broken_ciphertext_does_not_break_reading(self):
        """密钥不匹配时列表接口不应 500，而是记录错误并返回空串。"""
        instance = make_instance(password="Will#Break1")
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE inventory_dbinstance SET password_ciphertext = %s WHERE id = %s",
                ["enc:v1:broken-token", instance.pk],
            )
        with self.assertLogs("inventory.fields", level="ERROR"):
            instance.refresh_from_db()
            self.assertEqual(instance.password, "")

    def test_broken_ciphertext_survives_full_save(self):
        """解密失败后仍然全量 save()，绝不能把原密文覆盖成空（会永久丢密码）。"""
        instance = make_instance(password="Will#Break1")
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE inventory_dbinstance SET password_ciphertext = %s WHERE id = %s",
                ["enc:v1:broken-token", instance.pk],
            )

        with self.assertLogs("inventory.fields", level="ERROR"):
            instance.refresh_from_db()
        instance.name = "renamed-after-key-error"
        instance.save()  # 全量保存，会写回 password_ciphertext 列

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT password_ciphertext, name FROM inventory_dbinstance WHERE id = %s",
                [instance.pk],
            )
            raw, name = cursor.fetchone()

        self.assertEqual(raw, "enc:v1:broken-token", "原密文被覆盖了，密钥修好后就再也解不出来")
        self.assertEqual(name, "renamed-after-key-error", "其它字段仍应正常保存")
