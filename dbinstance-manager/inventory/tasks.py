"""Celery 定时任务。

- rotate_all_instance_passwords：每 12 小时（00:00 / 12:00）轮换所有启用实例的密码
- collect_daily_instance_stats：每天 00:00 按 (部门, 集群) 统计实例数量并落库

两个任务都做了幂等处理：重复执行不会产生重复数据（统计用 update_or_create，
密码轮换靠版本号 + 唯一约束保证不会写出两条同版本历史）。
"""
from __future__ import annotations

import logging
from datetime import date, datetime

from celery import shared_task
from django.db import transaction
from django.db.models import Count, F, Q, Sum
from django.utils import timezone

from .models import Cluster, DBInstance, InstanceDailyStat, PasswordRotationConflict

logger = logging.getLogger(__name__)

ROTATE_TASK_NAME = "inventory.tasks.rotate_all_instance_passwords"
STATS_TASK_NAME = "inventory.tasks.collect_daily_instance_stats"


def run_sync(task, **kwargs):
    """在当前进程内同步执行任务体。

    供管理命令 / 本地验证使用：不经过 broker，也不需要 result backend。
    （`task.apply()` 会访问 result backend，本机没装 redis 客户端时会直接报错，
    所以这里用 `task.run()`——它就是被 @shared_task 包装的那个函数本身。）
    """
    return task.run(**kwargs)


def _parse_date(value) -> date:
    if value in (None, ""):
        return timezone.localdate()
    if isinstance(value, datetime):
        return timezone.localtime(value).date() if timezone.is_aware(value) else value.date()
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value), "%Y-%m-%d").date()


@shared_task(bind=True, name=ROTATE_TASK_NAME)
def rotate_all_instance_passwords(
    self,
    reason: str = "scheduled",
    instance_ids: list[int] | None = None,
    batch_size: int = 200,
    max_conflict_retries: int = 3,
) -> dict:
    """轮换密码。返回统计信息，**不返回任何明文密码**。

    覆盖**所有实例**（含已停用的）：停用只是"不再对外提供服务"，
    但账号还活着、凭据仍然有效，不轮换就是长期不换的静态口令。
    如果确实要跳过停用实例，把下面的 `.filter(is_active=True)` 加回来即可。
    """
    queryset = DBInstance.objects.all().order_by("id")
    if instance_ids:
        queryset = queryset.filter(id__in=instance_ids)

    total = rotated = failed = 0
    errors: list[dict] = []

    for instance in queryset.iterator(chunk_size=batch_size):
        total += 1
        for attempt in range(1, max_conflict_retries + 1):
            try:
                instance.rotate_password(reason=reason)
                rotated += 1
                break
            except PasswordRotationConflict:
                # 另一个 worker 抢先轮换了：重新加载版本号后重试
                instance.refresh_from_db(
                    fields=["password_ciphertext", "password_version", "password_rotated_at"]
                )
                logger.warning(
                    "密码轮换版本冲突，重试 instance_id=%s attempt=%s", instance.pk, attempt
                )
                if attempt == max_conflict_retries:
                    failed += 1
                    errors.append({"instance_id": instance.pk, "error": "版本冲突，重试后仍失败"})
            except Exception as exc:  # noqa: BLE001 - 单个实例失败不应中断整批
                failed += 1
                errors.append({"instance_id": instance.pk, "error": str(exc)[:200]})
                logger.exception("密码轮换失败 instance_id=%s", instance.pk)
                break

    summary = {
        "task": ROTATE_TASK_NAME,
        "reason": reason,
        "total": total,
        "rotated": rotated,
        "failed": failed,
        "errors": errors[:20],
        "finished_at": timezone.localtime(timezone.now()).isoformat(),
    }
    logger.info("密码轮换完成 %s", {k: v for k, v in summary.items() if k != "errors"})
    return summary


@shared_task(bind=True, name=STATS_TASK_NAME)
def collect_daily_instance_stats(self, stat_date: str | None = None) -> dict:
    """按 (部门, 集群) 统计实例数量并写入 InstanceDailyStat。"""
    day = _parse_date(stat_date)

    clusters = Cluster.objects.select_related("department").annotate(
        total=Count("instances", distinct=True),
        active=Count("instances", filter=Q(instances__is_active=True), distinct=True),
        online=Count(
            "instances", filter=Q(instances__status=DBInstance.Status.ONLINE), distinct=True
        ),
    )

    created = updated = 0
    seen_cluster_ids: list[int] = []
    total_instances = 0

    with transaction.atomic():
        for cluster in clusters:
            _, is_created = InstanceDailyStat.objects.update_or_create(
                stat_date=day,
                department_id=cluster.department_id,
                cluster_id=cluster.pk,
                defaults={
                    "total_instances": cluster.total,
                    "active_instances": cluster.active,
                    "online_instances": cluster.online,
                },
            )
            created += int(is_created)
            updated += int(not is_created)
            seen_cluster_ids.append(cluster.pk)
            total_instances += cluster.total

        # 幂等：同一日期重复执行时，清掉已不存在集群的残留行
        removed, _ = (
            InstanceDailyStat.objects.filter(stat_date=day)
            .exclude(cluster_id__in=seen_cluster_ids)
            .delete()
        )

    summary = {
        "task": STATS_TASK_NAME,
        "stat_date": day.isoformat(),
        "clusters": len(seen_cluster_ids),
        "created": created,
        "updated": updated,
        "removed": removed,
        "total_instances": total_instances,
        "finished_at": timezone.localtime(timezone.now()).isoformat(),
    }
    logger.info("每日统计完成 %s", summary)
    return summary


@shared_task(name="inventory.tasks.daily_summary_by_department")
def daily_summary_by_department(stat_date: str | None = None) -> dict:
    """附加任务示例：从已落库的统计表里再按部门汇总（供报表使用）。"""
    day = _parse_date(stat_date)
    rows = (
        InstanceDailyStat.objects.filter(stat_date=day)
        .values(
            "department_id",
            department_name=F("department__name"),
            department_code=F("department__code"),
        )
        .annotate(
            clusters=Count("cluster", distinct=True),
            total_instances=Sum("total_instances"),
            online_instances=Sum("online_instances"),
        )
        .order_by("department_code")
    )
    return {"stat_date": day.isoformat(), "departments": list(rows)}
