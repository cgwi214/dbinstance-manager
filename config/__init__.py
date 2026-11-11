"""让 Celery app 在 Django 启动时一并加载，保证 @shared_task 能找到它。"""

from .celery import app as celery_app

__all__ = ("celery_app",)
