#!/usr/bin/env python
"""端到端冒烟脚本（只用标准库，对运行中的服务发真实 HTTP 请求）。

它复现了 docs/VERIFY.md 里的手工验证，并把关键不变量变成断言：

    python scripts/e2e_check.py                          # 默认 http://127.0.0.1:8000
    python scripts/e2e_check.py --base-url http://127.0.0.1:8765
    python scripts/e2e_check.py --cleanup                # 额外清理本次创建的资源

先启动服务：
    python manage.py migrate && python manage.py runserver 8000

建议对着一个临时库跑，避免污染开发数据：
    DB_PATH=var/e2e.sqlite3 python manage.py migrate
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
from datetime import date

RESULTS: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, detail: str = "") -> bool:
    RESULTS.append((bool(ok), name, detail))
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name}" + (f" — {detail}" if detail else ""))
    return bool(ok)


class Api:
    def __init__(self, base_url: str, token: str = ""):
        self.base_url = base_url.rstrip("/")
        self.token = token.strip()

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Token {self.token}"
        return headers

    def call(self, method: str, path: str, body=None, timeout: float = 30.0):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            self.base_url + path, data=data, method=method, headers=self._headers()
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8") or "{}"
                return response.status, json.loads(raw), dict(response.headers)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8") or "{}"
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = {"raw": raw[:200]}
            return exc.code, payload, dict(exc.headers)


def wait_until_ready(api: Api, attempts: int = 40) -> bool:
    for _ in range(attempts):
        try:
            status, _, _ = api.call("GET", "/api/health/", timeout=3)
            if status == 200:
                return True
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError):
            pass
        time.sleep(0.5)
    return False


def open_listener() -> tuple[socket.socket, int]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(8)
    return sock, sock.getsockname()[1]


def closed_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def run(api: Api, cleanup: bool) -> int:
    stamp = str(int(time.time()))
    dept_code = f"E2E{stamp}"
    cluster_code = f"e2e-{stamp}"
    created: dict[str, int] = {}

    print("\n=== 1. 健康检查 / 请求耗时头 ===")
    status, body, headers = api.call("GET", "/api/health/")
    check(status == 200 and body.get("database") is True, "服务与数据库可用", f"status={status}")
    duration = headers.get("X-Request-Duration-Ms")
    check(duration is not None and float(duration) >= 0, "响应头带请求耗时", f"{duration} ms")
    check("Server-Timing" in headers, "响应头带 Server-Timing", headers.get("Server-Timing", ""))

    print("\n=== 2. 部门 / 集群 / 实例 CRUD ===")
    status, dept, _ = api.call(
        "POST", "/api/departments/", {"name": f"E2E 部门 {stamp}", "code": dept_code.lower()}
    )
    check(status == 201, "创建部门", f"status={status}")
    if status != 201:
        return 1
    created["department"] = dept["id"]
    check(dept["code"] == dept_code, "部门编码被规范为大写", dept["code"])

    status, cluster, _ = api.call(
        "POST",
        "/api/clusters/",
        {
            "name": f"E2E 集群 {stamp}",
            "code": cluster_code,
            "department": dept["id"],
            "engine": "mysql",
            "environment": "test",
            "vip": "127.0.0.1",
            "port": 3306,
        },
    )
    check(status == 201, "创建集群", f"status={status}")
    if status != 201:
        return 1
    created["cluster"] = cluster["id"]
    check(cluster["department_code"] == dept_code, "集群带出部门编码", cluster["department_code"])

    listener, live_port = open_listener()
    status, instance, _ = api.call(
        "POST",
        "/api/instances/",
        {
            "name": f"e2e-{stamp}",
            "cluster": cluster["id"],
            "host": "127.0.0.1",
            "port": live_port,
            "account": "app_rw",
            "password": "E2e#Passw0rd9",
        },
    )
    check(status == 201, "创建实例", f"status={status}")
    if status != 201:
        listener.close()
        return 1
    created["instance"] = instance["id"]
    check("password" not in instance and "password_ciphertext" not in instance, "响应不含任何密码字段")
    check(instance["password_version"] == 1, "初始密码版本为 1", str(instance["password_version"]))
    check(instance["endpoint"] == f"127.0.0.1:{live_port}", "endpoint 拼接正确", instance["endpoint"])

    status, listing, _ = api.call("GET", f"/api/instances/?department={dept['id']}")
    names = [row["name"] for row in listing.get("results", [])]
    check(status == 200 and instance["name"] in names, "按部门过滤能查到新实例", str(names))

    status, body, _ = api.call("GET", f"/api/clusters/{cluster['id']}/")
    check(body.get("instance_count") == 1, "集群实例数注解正确", str(body.get("instance_count")))

    status, body, _ = api.call("DELETE", f"/api/clusters/{cluster['id']}/")
    check(status == 409, "删除仍被引用的集群返回 409", f"status={status}")

    print("\n=== 3. TCP 端口探测（真实监听 socket）===")
    status, body, _ = api.call("POST", f"/api/instances/{instance['id']}/probe/", {"timeout": 2})
    check(status == 200 and body.get("reachable") is True, "可达端口探测为 reachable", str(body.get("latency_ms")))
    check(body.get("resolved_ip") == "127.0.0.1", "返回解析到的 IP", str(body.get("resolved_ip")))

    status, dead, _ = api.call(
        "POST",
        "/api/instances/",
        {
            "name": f"e2e-dead-{stamp}",
            "cluster": cluster["id"],
            "host": "127.0.0.1",
            "port": closed_port(),
        },
    )
    created["dead_instance"] = dead.get("id")
    if not created["dead_instance"]:
        check(False, "创建不可达实例", f"status={status}")
        listener.close()
        return 1
    status, body, _ = api.call("POST", f"/api/instances/{created['dead_instance']}/probe/", {"timeout": 1})
    check(status == 200 and body.get("reachable") is False, "关闭端口探测为 unreachable", body.get("error", ""))
    check(bool(body.get("error")), "不可达时带错误原因", body.get("error", ""))

    status, _, _ = api.call("POST", "/api/probe/", {"host": "127.0.0.1", "port": live_port})
    check(status == 404, "独立探测接口已下线（只保留实例级）", f"status={status}")
    listener.close()

    print("\n=== 4. 凭据 / 轮换 / 历史 ===")
    status, body, _ = api.call("POST", f"/api/instances/{instance['id']}/credential/", {})
    check(status == 400, "查看凭据缺少 reason 被拒绝", f"status={status}")

    status, body, _ = api.call(
        "POST", f"/api/instances/{instance['id']}/credential/", {"reason": "E2E 自动化验证"}
    )
    old_password = body.get("password", "")
    check(status == 200 and old_password == "E2e#Passw0rd9", "凭据接口返回明文", f"len={len(old_password)}")

    status, body, _ = api.call("POST", f"/api/instances/{instance['id']}/rotate-password/", {})
    new_password = body.get("password", "")
    check(status == 200 and new_password != old_password, "轮换后密码变化", f"版本={body.get('password_version')}")
    check(len(new_password) == 24, "新密码长度 24", str(len(new_password)))

    status, history, _ = api.call("GET", f"/api/instances/{instance['id']}/password-history/")
    versions = sorted(row["version"] for row in history) if isinstance(history, list) else []
    check(versions == [1, 2], "密码历史记录两个版本", str(versions))
    check(
        all("password" not in row and "password_ciphertext" not in row for row in history or []),
        "历史接口不含明文",
    )

    print("\n=== 5. 每日统计（与实时 API 交叉核对）===")
    today = date.today().isoformat()
    status, body, _ = api.call("POST", "/api/stats/daily/collect/", {"stat_date": today})
    check(status in (200, 202), "触发统计任务", f"status={status} state={body.get('state')}")

    if status == 202:
        # 异步模式：等 worker 真正落库，再继续做交叉核对（否则会把「还没跑」误判成「统计错」）
        waited, rows = 0, 0
        while waited < 20:
            _, polled, _ = api.call("GET", f"/api/stats/daily/?date={today}")
            rows = polled.get("count", 0)
            if rows:
                break
            time.sleep(1)
            waited += 1
        check(rows > 0, "worker 已异步落库", f"{rows} 行，等待 {waited}s")

    status, summary, _ = api.call("GET", f"/api/stats/daily/summary/?date={date.today().isoformat()}")
    check(status == 200, "查询统计汇总", f"status={status} 部门数={len(summary.get('departments', []))}")

    if status == 200 and summary.get("departments"):
        mismatched = []
        for row in summary["departments"]:
            _, live, _ = api.call("GET", f"/api/instances/?department={row['department_id']}")
            if live.get("count") != row["total_instances"]:
                mismatched.append((row["department_code"], row["total_instances"], live.get("count")))
        check(not mismatched, "统计数字与实例接口一致", str(mismatched) if mismatched else "全部一致")

    print("\n=== 6. 请求耗时落库 ===")
    status, body, _ = api.call("GET", "/api/request-logs/stats/")
    check(status == 200 and body.get("count", 0) > 0, "请求耗时统计有数据", json.dumps(body, ensure_ascii=False))

    print("\n=== 7. 清理 ===")
    if cleanup:
        targets = [
            ("dead_instance", "instances"),
            ("instance", "instances"),
            ("cluster", "clusters"),
            ("department", "departments"),
        ]
        for key, resource in targets:
            pk = created.get(key)
            if not pk:
                continue
            status, _, _ = api.call("DELETE", f"/api/{resource}/{pk}/")
            if status == 204:
                check(True, f"清理 {key}", "已删除")
            elif status == 409:
                # 统计表对集群/部门是 PROTECT，这是设计使然，不算失败
                print(f"[SKIP] 清理 {key} — 已被每日统计引用（PROTECT），保留记录")
            else:
                check(False, f"清理 {key}", f"status={status}")
    else:
        print(
            f"      保留本次创建的资源：部门 {created['department']} / 集群 {created['cluster']} "
            f"/ 实例 {created['instance']}（加 --cleanup 自动删除）"
        )

    failed = [row for row in RESULTS if not row[0]]
    print("\n" + "=" * 60)
    print(f"共 {len(RESULTS)} 项断言，通过 {len(RESULTS) - len(failed)} 项，失败 {len(failed)} 项")
    if failed:
        for _, name, detail in failed:
            print(f"  FAIL: {name} — {detail}")
        return 1
    print("全部通过 ✅")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="数据库实例管理系统端到端冒烟")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--token",
        default=os.environ.get("API_TOKEN", ""),
        help="API_AUTH_REQUIRED=1 时必填，也可用环境变量 API_TOKEN",
    )
    parser.add_argument("--cleanup", action="store_true", help="结束后删除本次创建的资源")
    args = parser.parse_args()

    api = Api(args.base_url, token=args.token)
    print(f"目标服务：{api.base_url}" + ("（带 Token）" if api.token else ""))
    if not wait_until_ready(api):
        print(
            "服务不可达。请先启动：\n"
            "    python manage.py migrate\n"
            "    python manage.py runserver 8000\n"
            "无 broker 环境建议：CELERY_TASK_ALWAYS_EAGER=1 python manage.py runserver 8000"
        )
        return 2
    probe_status = api.call("GET", "/api/instances/")[0]
    if probe_status == 401:
        print(
            "服务已开启鉴权（API_AUTH_REQUIRED=1），但没拿到可用 Token。\n"
            "    先生成：python manage.py create_api_token --username ops\n"
            "    再运行：python scripts/e2e_check.py --token <token>"
        )
        return 2
    return run(api, args.cleanup)


if __name__ == "__main__":
    sys.exit(main())
