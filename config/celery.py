"""Celery 应用配置。

worker:  celery -A config worker -l info
beat:    celery -A config beat -l info
"""
from __future__ import annotations

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("dbinstance")

# 所有 CELERY_* 配置项都从 Django settings 读取（namespace 机制）
app.config_from_object("django.conf:settings", namespace="CELERY")

# 自动发现各 app 下的 tasks.py
app.autodiscover_tasks()


@app.task(bind=True, name="config.debug_task")
def debug_task(self) -> str:  # pragma: no cover - 调试用
    return f"celery ok, request={self.request!r}"
