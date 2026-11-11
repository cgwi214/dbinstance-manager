"""手动触发每日统计（不需要 broker / Redis，进程内同步执行）。

用法：
    python manage.py collect_stats
    python manage.py collect_stats --date 2026-11-09
"""
from django.core.management.base import BaseCommand

from inventory.tasks import collect_daily_instance_stats, run_sync


class Command(BaseCommand):
    help = "按部门和集群统计实例数量并写入统计表"

    def add_arguments(self, parser):
        parser.add_argument("--date", default=None, help="统计日期 YYYY-MM-DD，默认今天")

    def handle(self, *args, **options):
        summary = run_sync(collect_daily_instance_stats, stat_date=options.get("date"))
        self.stdout.write(self.style.SUCCESS(f"统计完成: {summary}"))
