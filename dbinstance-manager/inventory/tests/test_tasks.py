"""Celery 任务测试：密码轮换 + 每日统计（含幂等性与调度配置）。"""
from __future__ import annotations

from datetime import date

from django.conf import settings
from unittest import mock

from django.test import TestCase

from config.celery import app as celery_app
from inventory.models import (
    DBInstance,
    InstanceDailyStat,
    InstancePasswordHistory,
    PasswordRotationConflict,
)
from inventory.tasks import collect_daily_instance_stats, rotate_all_instance_passwords
from inventory.tests.factories import make_cluster, make_department, make_instance


class BeatScheduleTests(TestCase):
    def test_rotation_runs_every_12_hours(self):
        entry = settings.CELERY_BEAT_SCHEDULE["rotate-instance-passwords"]
        schedule = entry["schedule"]
        self.assertEqual(entry["task"], "inventory.tasks.rotate_all_instance_passwords")
        self.assertEqual(schedule.hour, {0, 12})
        self.assertEqual(schedule.minute, {0})

    def test_stats_runs_daily_at_midnight(self):
        entry = settings.CELERY_BEAT_SCHEDULE["collect-daily-instance-stats"]
        schedule = entry["schedule"]
        self.assertEqual(entry["task"], "inventory.tasks.collect_daily_instance_stats")
        self.assertEqual(schedule.hour, {0})
        self.assertEqual(schedule.minute, {0})

    def test_tasks_registered_in_celery_app(self):
        registered = celery_app.tasks
        self.assertIn("inventory.tasks.rotate_all_instance_passwords", registered)
        self.assertIn("inventory.tasks.collect_daily_instance_stats", registered)


class RotatePasswordsTaskTests(TestCase):
    def setUp(self):
        self.department = make_department()
        self.cluster = make_cluster(self.department)
        self.active_one = make_instance(self.cluster, name="a-1", host="10.0.0.11", port=3306)
        self.active_two = make_instance(self.cluster, name="a-2", host="10.0.0.12", port=3306)
        self.inactive = make_instance(
            self.cluster, name="a-3", host="10.0.0.13", port=3306, is_active=False
        )

    def test_rotates_all_instances_including_inactive(self):
        """停用只是"不再对外服务"，凭据仍然有效，所以也要轮换。"""
        old_passwords = {instance.pk: instance.password for instance in DBInstance.objects.all()}

        summary = rotate_all_instance_passwords(reason="scheduled")

        self.assertEqual(summary["total"], 3)
        self.assertEqual(summary["rotated"], 3)
        self.assertEqual(summary["failed"], 0)

        for instance in (self.active_one, self.active_two, self.inactive):
            fresh = DBInstance.objects.get(pk=instance.pk)
            self.assertEqual(fresh.password_version, 2)
            self.assertNotEqual(fresh.password, old_passwords[instance.pk])
            self.assertEqual(
                fresh.password_history.filter(version=2, reason="scheduled").count(), 1
            )

    def test_summary_never_contains_plaintext(self):
        summary = rotate_all_instance_passwords()
        serialised = str(summary)
        for instance in (self.active_one, self.active_two):
            self.assertNotIn(DBInstance.objects.get(pk=instance.pk).password, serialised)

    def test_rotation_is_idempotent_in_terms_of_history(self):
        rotate_all_instance_passwords()
        rotate_all_instance_passwords()

        self.assertEqual(
            InstancePasswordHistory.objects.filter(instance=self.active_one).count(), 3
        )
        # 每个版本只有一条历史（唯一约束保证）
        versions = list(
            InstancePasswordHistory.objects.filter(instance=self.active_one).values_list(
                "version", flat=True
            )
        )
        self.assertEqual(len(versions), len(set(versions)))

    def test_rotate_subset_by_instance_ids(self):
        summary = rotate_all_instance_passwords(instance_ids=[self.active_one.pk])
        self.assertEqual(summary["total"], 1)
        self.assertEqual(summary["rotated"], 1)
        self.assertEqual(DBInstance.objects.get(pk=self.active_two.pk).password_version, 1)

    def test_retries_after_version_conflict(self):
        """第一次抛冲突 → 重新加载版本 → 第二次成功，整体算 1 个成功。"""
        original = DBInstance.rotate_password
        calls = {"n": 0}

        def flaky(instance, reason="scheduled"):
            calls["n"] += 1
            if calls["n"] == 1:
                # 模拟别的 worker 抢先推进了版本
                type(instance).objects.filter(pk=instance.pk).update(
                    password_version=instance.password_version + 3
                )
                raise PasswordRotationConflict("模拟冲突")
            return original(instance, reason=reason)

        with mock.patch.object(DBInstance, "rotate_password", flaky):
            summary = rotate_all_instance_passwords(instance_ids=[self.active_one.pk])

        self.assertEqual(calls["n"], 2, "应当重试一次")
        self.assertEqual(summary["rotated"], 1)
        self.assertEqual(summary["failed"], 0)

    def test_reports_failure_when_retries_exhausted(self):
        with mock.patch.object(
            DBInstance, "rotate_password", side_effect=PasswordRotationConflict("总是冲突")
        ):
            summary = rotate_all_instance_passwords(
                instance_ids=[self.active_one.pk], max_conflict_retries=2
            )

        self.assertEqual(summary["rotated"], 0)
        self.assertEqual(summary["failed"], 1)
        self.assertTrue(summary["errors"])
        self.assertIn("instance_id", summary["errors"][0])

    def test_single_instance_failure_does_not_stop_batch(self):
        """一个实例炸了，其余实例仍要被轮换（批量任务的基本要求）。"""
        original = DBInstance.rotate_password

        def boom_for_first(instance, reason="scheduled"):
            if instance.pk == self.active_one.pk:
                raise RuntimeError("模拟单实例异常")
            return original(instance, reason=reason)

        with mock.patch.object(DBInstance, "rotate_password", boom_for_first):
            summary = rotate_all_instance_passwords()

        self.assertEqual(summary["total"], 3)
        self.assertEqual(summary["rotated"], 2)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(DBInstance.objects.get(pk=self.active_two.pk).password_version, 2)

    def test_no_instances_at_all(self):
        DBInstance.objects.all().delete()
        summary = rotate_all_instance_passwords()
        self.assertEqual(summary["total"], 0)
        self.assertEqual(summary["rotated"], 0)
        self.assertEqual(summary["failed"], 0)


class CollectDailyStatsTaskTests(TestCase):
    def setUp(self):
        self.dept_a = make_department(code="A", name="技术部")
        self.dept_b = make_department(code="B", name="财务部")

        self.cluster_a1 = make_cluster(self.dept_a, code="a-1")
        self.cluster_a2 = make_cluster(self.dept_a, code="a-2")  # 故意不放实例
        self.cluster_b1 = make_cluster(self.dept_b, code="b-1")

        make_instance(self.cluster_a1, name="a1-1", host="10.0.1.1", port=3306)
        make_instance(
            self.cluster_a1, name="a1-2", host="10.0.1.2", port=3306, status=DBInstance.Status.ONLINE
        )
        make_instance(
            self.cluster_b1,
            name="b1-1",
            host="10.0.2.1",
            port=5432,
            status=DBInstance.Status.ONLINE,
            is_active=False,
        )

    def test_groups_by_department_and_cluster(self):
        day = "2026-11-10"
        summary = collect_daily_instance_stats(stat_date=day)

        self.assertEqual(summary["stat_date"], day)
        self.assertEqual(summary["clusters"], 3)
        self.assertEqual(summary["total_instances"], 3)
        self.assertEqual(summary["created"], 3)
        self.assertEqual(summary["updated"], 0)

        rows = {
            (row.department.code, row.cluster.code): row
            for row in InstanceDailyStat.objects.filter(stat_date=day)
        }
        self.assertEqual(rows[("A", "a-1")].total_instances, 2)
        self.assertEqual(rows[("A", "a-1")].active_instances, 2)
        self.assertEqual(rows[("A", "a-1")].online_instances, 1)
        # 没有实例的集群也留一条 0 记录，便于做“集群数/覆盖”报表
        self.assertEqual(rows[("A", "a-2")].total_instances, 0)
        self.assertEqual(rows[("B", "b-1")].total_instances, 1)
        self.assertEqual(rows[("B", "b-1")].active_instances, 0)

    def test_rerun_updates_instead_of_duplicating(self):
        day = "2026-11-10"
        collect_daily_instance_stats(stat_date=day)
        make_instance(self.cluster_a1, name="a1-3", host="10.0.1.3", port=3306)

        summary = collect_daily_instance_stats(stat_date=day)

        self.assertEqual(summary["created"], 0)
        self.assertEqual(summary["updated"], 3)
        self.assertEqual(InstanceDailyStat.objects.filter(stat_date=day).count(), 3)
        row = InstanceDailyStat.objects.get(stat_date=day, cluster=self.cluster_a1)
        self.assertEqual(row.total_instances, 3)

    def test_different_days_are_independent(self):
        collect_daily_instance_stats(stat_date="2026-11-09")
        collect_daily_instance_stats(stat_date="2026-11-10")
        self.assertEqual(InstanceDailyStat.objects.count(), 6)
        self.assertEqual(
            InstanceDailyStat.objects.filter(stat_date=date(2026, 11, 9)).count(), 3
        )

    def test_default_date_is_today(self):
        from django.utils import timezone

        summary = collect_daily_instance_stats()
        self.assertEqual(summary["stat_date"], timezone.localdate().isoformat())

    def test_invalid_date_raises(self):
        with self.assertRaises(ValueError):
            collect_daily_instance_stats(stat_date="2026-13-40")

    def test_counts_follow_instance_changes(self):
        day = "2026-11-10"
        collect_daily_instance_stats(stat_date=day)
        DBInstance.objects.filter(cluster=self.cluster_a1).update(is_active=False)
        collect_daily_instance_stats(stat_date=day)
        row = InstanceDailyStat.objects.get(stat_date=day, cluster=self.cluster_a1)
        self.assertEqual(row.total_instances, 2)
        self.assertEqual(row.active_instances, 0)
