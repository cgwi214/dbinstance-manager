"""模型行为测试：密码版本、轮换、探测回写、部门层级校验。"""
from __future__ import annotations

from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from inventory.models import (
    Cluster,
    DBInstance,
    InstancePasswordHistory,
    PasswordRotationConflict,
)
from inventory.tests.factories import make_cluster, make_department, make_instance


class DepartmentTests(TestCase):
    def test_full_path(self):
        root = make_department(code="ROOT", name="总部")
        child = make_department(code="TECH", name="技术部", parent=root)
        leaf = make_department(code="DBA", name="DBA 组", parent=child)
        self.assertEqual(leaf.full_path, "总部 / 技术部 / DBA 组")

    def test_self_parent_rejected(self):
        dept = make_department()
        dept.parent = dept
        with self.assertRaises(ValidationError):
            dept.clean()

    def test_cycle_rejected(self):
        a = make_department(code="A")
        b = make_department(code="B", parent=a)
        a.parent = b  # A -> B -> A
        with self.assertRaises(ValidationError):
            a.clean()

    def test_code_unique(self):
        make_department(code="DUP")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                make_department(code="DUP")


class ClusterTests(TestCase):
    def test_name_unique_within_department_only(self):
        dept_a = make_department(code="A")
        dept_b = make_department(code="B")
        make_cluster(department=dept_a, code="a-1", name="同名集群")
        # 不同部门可以同名
        make_cluster(department=dept_b, code="b-1", name="同名集群")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                make_cluster(department=dept_a, code="a-2", name="同名集群")


class DBInstancePasswordTests(TestCase):
    def test_initial_password_creates_version_and_history(self):
        instance = make_instance(password="First#Pass1")
        self.assertEqual(instance.password_version, 1)
        self.assertTrue(instance.check_password("First#Pass1"))
        self.assertFalse(instance.check_password("wrong"))
        history = instance.password_history.all()
        self.assertEqual(history.count(), 1)
        self.assertEqual(history.first().version, 1)
        self.assertEqual(history.first().reason, InstancePasswordHistory.Reason.MANUAL)

    def test_rotate_bumps_version_and_appends_history(self):
        instance = make_instance(password="First#Pass1")
        new_password = instance.rotate_password()

        self.assertNotEqual(new_password, "First#Pass1")
        self.assertEqual(instance.password_version, 2)
        self.assertTrue(instance.check_password(new_password))
        self.assertFalse(instance.check_password("First#Pass1"))
        self.assertEqual(instance.password_history.count(), 2)
        self.assertEqual(
            instance.password_history.order_by("version").last().reason,
            InstancePasswordHistory.Reason.SCHEDULED,
        )

    def test_rotate_persists_to_database(self):
        instance = make_instance(password="First#Pass1")
        new_password = instance.rotate_password()
        fresh = DBInstance.objects.get(pk=instance.pk)
        self.assertEqual(fresh.password_version, 2)
        self.assertTrue(fresh.check_password(new_password))

    def test_password_is_stale(self):
        instance = make_instance(password="First#Pass1")
        self.assertFalse(instance.password_is_stale)

        DBInstance.objects.filter(pk=instance.pk).update(
            password_rotated_at=timezone.now() - timedelta(hours=13)
        )
        instance.refresh_from_db()
        self.assertTrue(instance.password_is_stale)

    def test_check_password_supports_non_ascii(self):
        """compare_digest 对含非 ASCII 的 str 会抛 TypeError，必须转 bytes 比较。"""
        instance = make_instance(password="P@ss中文密码9")

        self.assertTrue(instance.check_password("P@ss中文密码9"))
        self.assertFalse(instance.check_password("P@ss中文密码8"))

    def test_stale_object_rotation_raises_conflict(self):
        """两个进程各自加载同一实例后再轮换：后手必须冲突，而不是覆盖。"""
        instance = make_instance(password="First#Pass1")
        stale = DBInstance.objects.get(pk=instance.pk)  # 另一份副本，版本也是 1

        instance.rotate_password()

        with self.assertRaises(PasswordRotationConflict):
            stale.rotate_password()

        fresh = DBInstance.objects.get(pk=instance.pk)
        self.assertEqual(fresh.password_version, 2, "冲突时不应推进版本")
        self.assertEqual(
            InstancePasswordHistory.objects.filter(instance_id=instance.pk).count(),
            2,
            "冲突不应留下多余的历史记录",
        )

    def test_conflict_does_not_change_password(self):
        instance = make_instance(password="First#Pass1")
        stale = DBInstance.objects.get(pk=instance.pk)  # 必须先加载，才是"旧版本副本"
        winner_password = instance.rotate_password()

        with self.assertRaises(PasswordRotationConflict):
            stale.rotate_password(reason="scheduled")

        fresh = DBInstance.objects.get(pk=instance.pk)
        self.assertTrue(fresh.check_password(winner_password), "赢家的密码不能被覆盖")

    def test_host_port_unique(self):
        first = make_instance(name="i-1", host="10.0.0.9", port=3306)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                make_instance(
                    cluster=first.cluster, name="i-2", host="10.0.0.9", port=3306
                )


class ProbeWriteBackTests(TestCase):
    def test_reachable_marks_online(self):
        from inventory.services import ProbeResult

        instance = make_instance()
        self.assertEqual(instance.status, DBInstance.Status.UNKNOWN)
        instance.record_probe(ProbeResult("10.0.0.1", 3306, True, 1.23, "", "10.0.0.1", 1.0))

        instance.refresh_from_db()
        self.assertEqual(instance.status, DBInstance.Status.ONLINE)
        self.assertTrue(instance.last_probe_reachable)
        self.assertEqual(instance.last_probe_latency_ms, 1.23)
        self.assertIsNotNone(instance.last_probe_at)

    def test_unreachable_keeps_maintenance_status(self):
        from inventory.services import ProbeResult

        instance = make_instance(status=DBInstance.Status.MAINTENANCE)
        instance.record_probe(ProbeResult("10.0.0.1", 3306, False, None, "连接被拒绝", None, 1.0))

        instance.refresh_from_db()
        self.assertEqual(instance.status, DBInstance.Status.MAINTENANCE)
        self.assertFalse(instance.last_probe_reachable)
        self.assertEqual(instance.last_probe_error, "连接被拒绝")

    def test_reachable_does_not_clear_maintenance_status(self):
        """维护中是人工资态，探测成功也不能把它改回 online。"""
        from inventory.services import ProbeResult

        instance = make_instance(status=DBInstance.Status.MAINTENANCE)
        instance.record_probe(ProbeResult("10.0.0.1", 3306, True, 1.2, "", "10.0.0.1", 1.0))

        instance.refresh_from_db()
        self.assertEqual(instance.status, DBInstance.Status.MAINTENANCE)
        self.assertTrue(instance.last_probe_reachable)

    def test_unreachable_marks_online_instance_offline(self):
        from inventory.services import ProbeResult

        instance = make_instance(status=DBInstance.Status.ONLINE)
        instance.record_probe(ProbeResult("10.0.0.1", 3306, False, None, "连接超时", None, 1.0))

        instance.refresh_from_db()
        self.assertEqual(instance.status, DBInstance.Status.OFFLINE)


class ClusterRelationTests(TestCase):
    def test_delete_protected(self):
        from django.db.models import ProtectedError

        cluster = make_cluster()
        make_instance(cluster=cluster)
        with self.assertRaises(ProtectedError):
            cluster.delete()

    def test_str(self):
        instance = make_instance(name="mysql-01", host="10.1.1.1", port=3307)
        self.assertEqual(str(instance), "mysql-01@10.1.1.1:3307")
        self.assertEqual(instance.endpoint, "10.1.1.1:3307")
        self.assertEqual(Cluster.objects.count(), 1)
