"""手动触发密码轮换（不需要 broker / Redis，进程内同步执行）。

用法：
    python manage.py rotate_passwords
    python manage.py rotate_passwords --reason manual --instance-id 3
"""
from django.core.management.base import BaseCommand

from inventory.tasks import rotate_all_instance_passwords, run_sync


class Command(BaseCommand):
    help = "立即轮换实例密码（默认只处理启用中的实例）"

    def add_arguments(self, parser):
        parser.add_argument("--reason", default="manual", help="轮换原因，写入密码历史")
        parser.add_argument(
            "--instance-id",
            action="append",
            type=int,
            dest="instance_ids",
            help="只轮换指定实例，可重复传多次",
        )

    def handle(self, *args, **options):
        summary = run_sync(
            rotate_all_instance_passwords,
            reason=options["reason"],
            instance_ids=options.get("instance_ids"),
        )
        self.stdout.write(self.style.SUCCESS(f"轮换完成: {summary}"))
