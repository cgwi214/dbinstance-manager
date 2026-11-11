from django.apps import AppConfig


class InventoryConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "inventory"
    verbose_name = "数据库实例管理"

    def ready(self) -> None:
        # 导入即注册部署安全检查（manage.py check --deploy）
        from . import checks

        checks  # noqa: B018
