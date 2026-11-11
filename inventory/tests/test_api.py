"""REST API 测试：CRUD、探测、凭据、轮换、统计、请求耗时。"""
from __future__ import annotations

import socket
from unittest import mock

from django.test import TestCase

from inventory.models import (
    Cluster,
    DBInstance,
    Department,
    InstanceDailyStat,
    InstancePasswordHistory,
)
from inventory.tasks import collect_daily_instance_stats
from inventory.tests.factories import make_cluster, make_department, make_instance


def _open_listener() -> tuple[socket.socket, int]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(8)
    return sock, sock.getsockname()[1]


def _closed_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class BaseAPITestCase(TestCase):
    def setUp(self):
        self.dept = make_department(code="TECH", name="技术部")
        self.fin_dept = make_department(code="FIN", name="财务部")
        self.cluster = make_cluster(self.dept, code="tech-mysql", name="技术部 MySQL")
        self.fin_cluster = make_cluster(
            self.fin_dept, code="fin-pg", name="财务部 PG", engine=Cluster.Engine.POSTGRESQL
        )
        self.instance = make_instance(
            self.cluster, name="i-1", host="10.0.0.1", port=3306, password="First#Pass1"
        )


class DepartmentAPITests(BaseAPITestCase):
    def test_create_uppercases_code(self):
        response = self.client.post(
            "/api/departments/", {"name": "研发部", "code": "rd"}, content_type="application/json"
        )
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.data["code"], "RD")
        self.assertTrue(Department.objects.filter(code="RD").exists())

    def test_create_duplicate_code_returns_400(self):
        response = self.client.post(
            "/api/departments/", {"name": "重复", "code": "TECH"}, content_type="application/json"
        )
        self.assertEqual(response.status_code, 400)

    def test_list_is_paginated_and_annotated(self):
        response = self.client.get("/api/departments/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("results", response.data)
        row = next(item for item in response.data["results"] if item["code"] == "TECH")
        self.assertEqual(row["cluster_count"], 1)
        self.assertEqual(row["instance_count"], 1)
        self.assertEqual(row["full_path"], "技术部")

    def test_retrieve_and_partial_update(self):
        response = self.client.get(f"/api/departments/{self.dept.pk}/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["name"], "技术部")

        response = self.client.patch(
            f"/api/departments/{self.dept.pk}/", {"owner": "王五"}, content_type="application/json"
        )
        self.assertEqual(response.status_code, 200)
        self.dept.refresh_from_db()
        self.assertEqual(self.dept.owner, "王五")

    def test_delete_empty_department(self):
        empty = make_department(code="EMPTY")
        response = self.client.delete(f"/api/departments/{empty.pk}/")
        self.assertEqual(response.status_code, 204)
        self.assertFalse(Department.objects.filter(pk=empty.pk).exists())

    def test_delete_department_with_cluster_returns_409(self):
        response = self.client.delete(f"/api/departments/{self.dept.pk}/")
        self.assertEqual(response.status_code, 409)
        self.assertTrue(Department.objects.filter(pk=self.dept.pk).exists())

    def test_parent_cycle_rejected(self):
        """A→B→A 的环必须被接口挡住，否则 full_path 会无限套娃。"""
        root = make_department(code="ROOT", name="总部")
        child = make_department(code="CHILD", name="子部门", parent=root)

        response = self.client.patch(
            f"/api/departments/{root.pk}/",
            {"parent": child.pk},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400, response.content)
        root.refresh_from_db()
        self.assertIsNone(root.parent_id)

    def test_page_size_param_is_honoured(self):
        """?page_size= 必须生效（DRF 默认不认这个参数，会静默忽略）。"""
        for index in range(3):
            make_department(code=f"PG{index}")

        response = self.client.get("/api/departments/?page_size=2")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data["results"]), 2)
        self.assertEqual(response.data["count"], 5)

        # 上限保护：请求超大页大小时被夹到 max_page_size，不会把库拉空
        response = self.client.get("/api/departments/?page_size=99999")
        self.assertEqual(response.status_code, 200)
        self.assertLessEqual(len(response.data["results"]), 200)

    def test_search_and_is_active_filter(self):
        make_department(code="OLD", name="已撤销部门", is_active=False)
        response = self.client.get("/api/departments/?is_active=false")
        codes = [row["code"] for row in response.data["results"]]
        self.assertEqual(codes, ["OLD"])

        response = self.client.get("/api/departments/?search=技术")
        codes = [row["code"] for row in response.data["results"]]
        self.assertEqual(codes, ["TECH"])


class ClusterAPITests(BaseAPITestCase):
    def test_create_cluster(self):
        response = self.client.post(
            "/api/clusters/",
            {
                "name": "新集群",
                "code": "tech-redis",
                "department": self.dept.pk,
                "engine": "redis",
                "environment": "test",
                "vip": "10.0.9.9",
                "port": 6379,
            },
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.data["department_code"], "TECH")
        self.assertEqual(Cluster.objects.count(), 3)

    def test_filter_by_department_and_engine(self):
        response = self.client.get(f"/api/clusters/?department={self.fin_dept.pk}")
        self.assertEqual([row["code"] for row in response.data["results"]], ["fin-pg"])

        response = self.client.get("/api/clusters/?engine=postgresql")
        self.assertEqual([row["code"] for row in response.data["results"]], ["fin-pg"])

    def test_instance_count_annotation(self):
        response = self.client.get(f"/api/clusters/{self.cluster.pk}/")
        self.assertEqual(response.data["instance_count"], 1)

    def test_delete_cluster_with_instance_returns_409(self):
        response = self.client.delete(f"/api/clusters/{self.cluster.pk}/")
        self.assertEqual(response.status_code, 409)

    def test_invalid_port_rejected(self):
        response = self.client.post(
            "/api/clusters/",
            {"name": "坏端口", "code": "bad-port", "department": self.dept.pk, "port": 99999},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("port", response.data)


class InstanceAPITests(BaseAPITestCase):
    def test_create_with_password(self):
        response = self.client.post(
            "/api/instances/",
            {
                "name": "i-new",
                "cluster": self.cluster.pk,
                "host": "10.0.0.20",
                "port": 3306,
                "account": "app_rw",
                "password": "Str0ng#Passw0rd",
            },
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        self.assertNotIn("password", response.data)
        self.assertNotIn("password_ciphertext", response.data)
        self.assertEqual(response.data["password_version"], 1)
        self.assertEqual(response.data["endpoint"], "10.0.0.20:3306")
        self.assertEqual(response.data["department_name"], "技术部")

        instance = DBInstance.objects.get(pk=response.data["id"])
        self.assertTrue(instance.check_password("Str0ng#Passw0rd"))

    def test_create_without_password_generates_one(self):
        response = self.client.post(
            "/api/instances/",
            {"name": "i-auto", "cluster": self.cluster.pk, "host": "10.0.0.21", "port": 3306},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        generated = response.data.get("generated_password")
        self.assertIsNotNone(generated)
        self.assertGreaterEqual(len(generated), 16)

        instance = DBInstance.objects.get(pk=response.data["id"])
        self.assertTrue(instance.check_password(generated))
        self.assertEqual(instance.password_history.count(), 1)

    def test_short_password_rejected(self):
        response = self.client.post(
            "/api/instances/",
            {
                "name": "i-short",
                "cluster": self.cluster.pk,
                "host": "10.0.0.22",
                "port": 3306,
                "password": "short",
            },
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("password", response.data)

    def test_duplicate_host_port_rejected(self):
        response = self.client.post(
            "/api/instances/",
            {"name": "i-dup", "cluster": self.cluster.pk, "host": "10.0.0.1", "port": 3306},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

    def test_patch_into_conflicting_host_port_rejected(self):
        """PATCH 撞 (host, port) 唯一约束时必须是 400，不能 500。

        POST 有 DRF 自动生成的唯一性校验器，PATCH 是部分更新——只改 host 时
        校验器拿不到 port，容易被放过，最后在数据库层炸成 IntegrityError。
        """
        other = make_instance(self.cluster, name="i-2", host="10.0.0.2", port=3306)

        response = self.client.patch(
            f"/api/instances/{other.pk}/",
            {"host": "10.0.0.1"},  # 与 i-1 的 10.0.0.1:3306 冲突
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400, response.content)
        other.refresh_from_db()
        self.assertEqual(other.host, "10.0.0.2", "冲突时不应写库")

    def test_list_never_exposes_password(self):
        response = self.client.get("/api/instances/")
        self.assertEqual(response.status_code, 200)
        row = response.data["results"][0]
        self.assertNotIn("password", row)
        self.assertNotIn("password_ciphertext", row)
        self.assertEqual(row["account"], "root")

    def test_filter_by_department_and_search(self):
        make_instance(self.fin_cluster, name="fin-1", host="10.0.2.1", port=5432)
        response = self.client.get(f"/api/instances/?department={self.fin_dept.pk}")
        self.assertEqual([row["name"] for row in response.data["results"]], ["fin-1"])

        response = self.client.get("/api/instances/?search=10.0.0.1")
        self.assertEqual([row["name"] for row in response.data["results"]], ["i-1"])

    def test_patch_password_bumps_version(self):
        response = self.client.patch(
            f"/api/instances/{self.instance.pk}/",
            {"password": "New#Passw0rd9"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.instance.refresh_from_db()
        self.assertEqual(self.instance.password_version, 2)
        self.assertTrue(self.instance.check_password("New#Passw0rd9"))
        self.assertEqual(self.instance.password_history.count(), 2)

    def test_patch_without_password_keeps_version(self):
        response = self.client.patch(
            f"/api/instances/{self.instance.pk}/",
            {"account": "app_ro"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.instance.refresh_from_db()
        self.assertEqual(self.instance.password_version, 1)
        self.assertEqual(self.instance.account, "app_ro")
        self.assertTrue(self.instance.check_password("First#Pass1"))

    def test_delete_instance(self):
        response = self.client.delete(f"/api/instances/{self.instance.pk}/")
        self.assertEqual(response.status_code, 204)
        self.assertFalse(DBInstance.objects.filter(pk=self.instance.pk).exists())
        self.assertEqual(InstancePasswordHistory.objects.count(), 0, "历史应随实例级联删除")

    def test_stale_filter(self):
        response = self.client.get("/api/instances/?stale=1")
        # 刚创建的实例密码是新的，不应出现在 stale 列表
        self.assertEqual(response.data["count"], 0)


class ProbeAPITests(BaseAPITestCase):
    def test_probe_reachable_and_write_back(self):
        listener, port = _open_listener()
        self.addCleanup(listener.close)
        instance = make_instance(self.cluster, name="i-live", host="127.0.0.1", port=port)

        response = self.client.post(f"/api/instances/{instance.pk}/probe/", {}, content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.data["reachable"])
        self.assertIsNotNone(response.data["latency_ms"])
        self.assertEqual(response.data["endpoint"], f"127.0.0.1:{port}")

        instance.refresh_from_db()
        self.assertTrue(instance.last_probe_reachable)
        self.assertEqual(instance.status, DBInstance.Status.ONLINE)

        # 手工序列化的时间要和 DRF 的 DateTimeField 用同一个时区
        from django.utils import timezone as dj_timezone

        offset = dj_timezone.localtime().strftime("%z")
        expected_suffix = f"{offset[:3]}:{offset[3:]}"
        self.assertTrue(
            response.data["probed_at"].endswith(expected_suffix),
            f"probed_at={response.data['probed_at']} 应为本地时区 {expected_suffix}",
        )

    def test_standalone_probe_endpoint_removed(self):
        """探测接口收敛成一个：独立地址探测已下线，只留实例级。"""
        response = self.client.post(
            "/api/probe/", {"host": "127.0.0.1", "port": 3306}, content_type="application/json"
        )
        self.assertEqual(response.status_code, 404)

    def test_probe_unreachable(self):
        instance = make_instance(
            self.cluster, name="i-dead", host="127.0.0.1", port=_closed_port()
        )
        response = self.client.post(
            f"/api/instances/{instance.pk}/probe/", {"timeout": 1}, content_type="application/json"
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["reachable"])
        self.assertTrue(response.data["error"])

        instance.refresh_from_db()
        self.assertFalse(instance.last_probe_reachable)


class CredentialAPITests(BaseAPITestCase):
    def test_requires_reason(self):
        response = self.client.post(
            f"/api/instances/{self.instance.pk}/credential/", {}, content_type="application/json"
        )
        self.assertEqual(response.status_code, 400)

    def test_returns_plaintext_with_reason(self):
        response = self.client.post(
            f"/api/instances/{self.instance.pk}/credential/",
            {"reason": "初始化应用连接池"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.data["account"], "root")
        self.assertEqual(response.data["password"], "First#Pass1")

    def test_get_not_allowed(self):
        response = self.client.get(f"/api/instances/{self.instance.pk}/credential/")
        self.assertEqual(response.status_code, 405)

    def test_rotate_password_action(self):
        response = self.client.post(
            f"/api/instances/{self.instance.pk}/rotate-password/", {}, content_type="application/json"
        )
        self.assertEqual(response.status_code, 200, response.content)
        new_password = response.data["password"]
        self.assertNotEqual(new_password, "First#Pass1")
        self.assertEqual(response.data["password_version"], 2)

        self.instance.refresh_from_db()
        self.assertTrue(self.instance.check_password(new_password))
        self.assertFalse(self.instance.check_password("First#Pass1"))

    def test_password_history_lists_metadata_only(self):
        self.client.post(
            f"/api/instances/{self.instance.pk}/rotate-password/", {}, content_type="application/json"
        )
        response = self.client.get(f"/api/instances/{self.instance.pk}/password-history/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 2)
        versions = sorted(row["version"] for row in response.data)
        self.assertEqual(versions, [1, 2])
        self.assertNotIn("password", response.data[0])
        self.assertNotIn("password_ciphertext", response.data[0])


class StatsAPITests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        make_instance(self.cluster, name="i-2", host="10.0.0.2", port=3306)
        make_instance(self.fin_cluster, name="f-1", host="10.0.2.1", port=5432)

    def test_collect_then_query(self):
        collect_daily_instance_stats(stat_date="2026-11-10")

        response = self.client.get("/api/stats/daily/?date=2026-11-10")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 2)
        rows = {(row["department_code"], row["cluster_code"]): row for row in response.data["results"]}
        self.assertEqual(rows[("TECH", "tech-mysql")]["total_instances"], 2)
        self.assertEqual(rows[("FIN", "fin-pg")]["total_instances"], 1)

    def test_summary_by_department(self):
        collect_daily_instance_stats(stat_date="2026-11-10")
        response = self.client.get("/api/stats/daily/summary/?date=2026-11-10")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["total_instances"], 3)
        departments = {row["department_code"]: row for row in response.data["departments"]}
        self.assertEqual(departments["TECH"]["total_instances"], 2)
        self.assertEqual(departments["TECH"]["clusters"], 1)
        self.assertEqual(departments["FIN"]["total_instances"], 1)
        # 不能把 ORM 的 values() 字段名（department__name）直接暴露给调用方
        self.assertNotIn("department__name", response.data["departments"][0])
        self.assertIn("department_name", response.data["departments"][0])

    def test_collect_endpoint_dispatches_task(self):
        fake = mock.Mock()
        fake.id = "task-abc"
        fake.state = "PENDING"
        fake.ready.return_value = False
        with mock.patch(
            "inventory.views.collect_daily_instance_stats.delay", return_value=fake
        ) as delay:
            response = self.client.post(
                "/api/stats/daily/collect/", {"stat_date": "2026-11-10"}, content_type="application/json"
            )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.data["task_id"], "task-abc")
        delay.assert_called_once_with("2026-11-10")

    def test_collect_endpoint_runs_sync_in_eager_mode(self):
        """无 broker 的本地模式下，接口直接同步执行任务并返回结果。"""
        from django.test import override_settings

        with override_settings(CELERY_TASK_ALWAYS_EAGER=True):
            response = self.client.post(
                "/api/stats/daily/collect/",
                {"stat_date": "2026-11-10"},
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.data["state"], "SUCCESS")
        self.assertEqual(response.data["result"]["total_instances"], 3)
        self.assertEqual(InstanceDailyStat.objects.filter(stat_date="2026-11-10").count(), 2)

    def test_empty_summary(self):
        response = self.client.get("/api/stats/daily/summary/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["total_instances"], 0)
        self.assertEqual(InstanceDailyStat.objects.count(), 0)


class RequestLogAPITests(BaseAPITestCase):
    def test_stats_endpoint(self):
        self.client.get("/api/instances/")
        self.client.get("/api/departments/")

        response = self.client.get("/api/request-logs/stats/")
        self.assertEqual(response.status_code, 200)
        self.assertGreaterEqual(response.data["count"], 2)
        self.assertIsNotNone(response.data["p95_ms"])

        response = self.client.get("/api/request-logs/?path=/api/instances/")
        self.assertGreaterEqual(response.data["count"], 1)
        self.assertIn("duration_ms", response.data["results"][0])


class HealthAPITests(BaseAPITestCase):
    def test_health(self):
        response = self.client.get("/api/health/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], "ok")
        self.assertTrue(response.data["database"])
