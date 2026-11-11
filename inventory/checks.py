"""部署安全检查（只在 `manage.py check --deploy` 时执行）。

不放进普通 check 的原因是：本地开发与单元测试需要“零配置可跑”，
而生产部署必须显式配置加密密钥，否则一旦 SECRET_KEY 变更，
数据库里所有加密的实例密码都会永久无法解密。
"""
from __future__ import annotations

from django.conf import settings
from django.core.checks import Error, Warning, register


@register(deploy=True)
def check_encryption_keys(app_configs, **kwargs):
    issues = []
    keys = getattr(settings, "INSTANCE_ENCRYPTION_KEYS", []) or []

    if not keys:
        issues.append(
            Error(
                "未配置 INSTANCE_ENCRYPTION_KEYS，正在使用由 SECRET_KEY 派生的密钥。",
                hint=(
                    "生成方式：python -c \"from cryptography.fernet import Fernet;"
                    "print(Fernet.generate_key().decode())\"；"
                    "把结果写入环境变量 INSTANCE_ENCRYPTION_KEYS（轮换时用逗号分隔多个，"
                    "新密钥放最前面）。"
                ),
                id="inventory.E001",
            )
        )
    elif len(keys) > 5:
        issues.append(
            Warning(
                f"INSTANCE_ENCRYPTION_KEYS 配置了 {len(keys)} 个密钥，解密会逐个尝试。",
                hint="密钥轮换完成后请及时移除旧密钥。",
                id="inventory.W001",
            )
        )

    broker = str(getattr(settings, "CELERY_BROKER_URL", ""))
    if not getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False) and any(
        host in broker for host in ("localhost", "127.0.0.1", "0.0.0.0")
    ):
        issues.append(
            Warning(
                "Celery broker 指向本机地址，生产环境可能连不上独立 broker。",
                hint="通过 CELERY_BROKER_URL 指定生产 Redis/RabbitMQ 地址。",
                id="inventory.W002",
            )
        )

    return issues
