# API 文档（示例均为真实响应）

## 通用约定

- Base URL：`http://<host>:<port>/api/`
- 请求/响应体：`application/json`
- 列表接口统一分页：`{"count": 12, "next": null, "previous": null, "results": [...]}`，页大小默认 20（`API_PAGE_SIZE`），
  可用 `?page_size=` 覆盖，上限 200（超过按 200 处理）
- 所有时间字段都是 **`Asia/Shanghai` 时区的 ISO 8601 字符串**（`2026-11-10T15:01:43.491072+08:00`）
- 每个响应都带耗时头：

```
X-Request-Duration-Ms: 2.67
Server-Timing: app;dur=2.67
```

- **鉴权**（`API_AUTH_REQUIRED=1` 时）：请求头 `Authorization: Token <token>`。
  匿名 → **401**；普通用户做写操作或看明文凭据 → **403**；`/api/health/` 不需要 token。
- 错误格式：字段校验错误按字段返回，业务错误用 `detail`

```json
{"password": ["请确保这个字段至少包含 8 个字符。"]}
{"detail": "该记录仍被 Cluster, InstanceDailyStat 引用，请先处理关联数据（或改为停用）"}
```

| 状态码 | 含义 |
|---|---|
| 200 / 201 / 204 | 成功 / 创建成功 / 删除成功 |
| 202 | 任务已投递（异步统计） |
| 400 | 参数校验失败 |
| 404 | 资源不存在 |
| 405 | 方法不允许（如用 GET 看凭据） |
| 409 | 被下游数据引用，无法删除 |

---

## 1. 部门 `/api/departments/`

`GET /api/departments/?search=技术&is_active=true&parent=null&ordering=code`

```json
{
  "count": 2,
  "next": null,
  "previous": null,
  "results": [
    {
      "id": 1,
      "name": "技术部",
      "code": "TECH",
      "parent": null,
      "parent_name": null,
      "full_path": "技术部",
      "owner": "张三",
      "description": "",
      "is_active": true,
      "cluster_count": 2,
      "instance_count": 3,
      "created_at": "2026-11-10T14:53:35.123456+08:00",
      "updated_at": "2026-11-10T14:53:35.123456+08:00"
    }
  ]
}
```

`POST /api/departments/`（`code` 会被规范成大写）

```json
{"name": "研发部", "code": "rd", "parent": 1, "owner": "李四"}
```

`PATCH /api/departments/{id}/` 支持局部更新；`DELETE` 时若仍有集群引用返回 **409**。

`parent` 不能是自己，也不能形成环（`A→B→A`），两种情况都返回 **400**：

```json
{"parent": ["部门层级存在循环引用"]}
```

---

## 2. 集群 `/api/clusters/`

`GET /api/clusters/?department=1&engine=mysql&environment=prod&is_active=true&search=order`

```json
{
  "count": 1,
  "next": null,
  "previous": null,
  "results": [
    {
      "id": 1,
      "name": "技术部 MySQL 生产集群",
      "code": "tech-mysql-prod",
      "department": 1,
      "department_name": "技术部",
      "department_code": "TECH",
      "engine": "mysql",
      "engine_display": "MySQL",
      "environment": "prod",
      "environment_display": "生产",
      "vip": "10.0.1.10",
      "port": 3306,
      "description": "",
      "is_active": true,
      "instance_count": 2,
      "created_at": "2026-11-10T14:53:35.123456+08:00",
      "updated_at": "2026-11-10T14:53:35.123456+08:00"
    }
  ]
}
```

`POST /api/clusters/`

```json
{"name": "技术部 Redis 测试集群", "code": "tech-redis-test", "department": 1,
 "engine": "redis", "environment": "test", "vip": "10.0.1.30", "port": 6379}
```

`engine`：`mysql` `postgresql` `redis` `mongodb` `oracle` `sqlserver`；
`environment`：`prod` `staging` `test` `dev`。`port` 超出 1–65535 返回 400。

---

## 3. 实例 `/api/instances/`

`GET /api/instances/?cluster=1&department=1&status=online&role=primary&is_active=true&search=10.0&stale=1`

`stale=1` 表示「密码超过 12 小时未轮换（含从未轮换）」。

```json
{
  "count": 1,
  "next": null,
  "previous": null,
  "results": [
    {
      "id": 4,
      "name": "fin-pg-01",
      "cluster": 3,
      "cluster_name": "财务部 PostgreSQL 集群",
      "department": 2,
      "department_name": "财务部",
      "role": "primary",
      "role_display": "主库",
      "host": "10.0.2.11",
      "port": 5432,
      "endpoint": "10.0.2.11:5432",
      "engine": "postgresql",
      "status": "unknown",
      "status_display": "未知",
      "account": "root",
      "password_version": 1,
      "password_rotated_at": "2026-11-10T15:00:27.648749+08:00",
      "password_is_stale": false,
      "last_probe_at": null,
      "last_probe_reachable": null,
      "last_probe_latency_ms": null,
      "last_probe_error": "",
      "is_active": true,
      "created_at": "2026-11-10T15:00:27.648830+08:00",
      "updated_at": "2026-11-10T15:00:27.648844+08:00"
    }
  ]
}
```

> 响应里**没有** `password` / `password_ciphertext` 字段，这是设计约束（有测试断言）。

### 3.1 创建（不传密码则自动生成）

`POST /api/instances/`

```json
{"name": "fin-pg-03", "cluster": 3, "host": "10.0.2.13", "port": 5432, "account": "app_rw"}
```

```json
{
  "id": 8,
  "name": "doc-sample",
  "cluster": 1,
  "cluster_name": "技术部 MySQL 生产集群",
  "department": 1,
  "department_name": "技术部",
  "role": "primary",
  "role_display": "主库",
  "host": "10.0.9.9",
  "port": 3306,
  "endpoint": "10.0.9.9:3306",
  "engine": "mysql",
  "status": "unknown",
  "status_display": "未知",
  "account": "app_rw",
  "password_version": 1,
  "password_rotated_at": "2026-11-10T15:01:51.963676+08:00",
  "password_is_stale": false,
  "last_probe_at": null,
  "last_probe_reachable": null,
  "last_probe_latency_ms": null,
  "last_probe_error": "",
  "is_active": true,
  "created_at": "2026-11-10T15:01:51.963889+08:00",
  "updated_at": "2026-11-10T15:01:51.963918+08:00",
  "generated_password": "bn8Yt-K=BMsEf_&TyQv%UD7*"
}
```

`generated_password` **只在自动生成密码的那次创建响应里出现一次**，之后无法再取回（只能走凭据接口或重新轮换）。

### 3.2 修改

`PATCH /api/instances/{id}/`

```json
{"account": "app_ro", "password": "New#Passw0rd9"}
```

- 带 `password` ⇒ 立即生效、`password_version + 1`、写一条 `reason=manual` 的历史；
- 不带 `password` ⇒ 版本不变、密码不动（有测试保证）。

---

## 4. TCP 端口探测

### 4.1 探测实例端口（会回写探测结果）

`POST /api/instances/{id}/probe/`

```json
{"timeout": 1}
```

```json
{
  "instance_id": 4,
  "name": "fin-pg-01",
  "endpoint": "10.0.2.11:5432",
  "host": "10.0.2.11",
  "port": 5432,
  "reachable": false,
  "latency_ms": null,
  "error": "连接超时（>1.0s）",
  "resolved_ip": null,
  "timeout": 1.0,
  "probed_at": "2026-11-10T15:01:43.491072+08:00"
}
```

可达时：

```json
{"reachable": true, "latency_ms": 19.877, "error": "", "resolved_ip": "10.0.1.11"}
```

探测结果同时写回实例：`last_probe_at` / `last_probe_reachable` / `last_probe_latency_ms` / `last_probe_error`，
并把 `status` 置为 `online`；**只有原本在线的实例**才会因探测失败被改判 `offline`。
`maintenance`（维护中）是人工状态，探测不会覆盖它——成功或失败都不改。

> 只有一个探测接口：`POST /api/instances/{id}/probe/`。
> 早期版本还有个不落库的 `/api/probe/`，已下线（现在返回 404）。

---

## 5. 凭据与密码轮换

### 5.1 查看当前凭据（必须填原因）

`POST /api/instances/{id}/credential/`

```json
{"reason": "初始化应用连接池"}
```

```json
{
  "instance_id": 4,
  "endpoint": "10.0.2.11:5432",
  "account": "root",
  "password": "Xx-*rhd+k8fAyU-a26+#=4aT",
  "password_version": 1,
  "password_rotated_at": "2026-11-10T15:00:27.648749+08:00"
}
```

- 只允许 `POST`（GET 返回 405），避免被浏览器/爬虫顺手取走；
- `reason` 少于 4 个字返回 400；
- 每次调用都会写一条 `WARNING` 审计日志（`instance_id` / `operator` / `reason`）。

### 5.2 立即轮换

`POST /api/instances/{id}/rotate-password/`（无请求体）

```json
{
  "instance_id": 4,
  "account": "root",
  "password": "q7$Hm2pR!vXc9tKd3wZ&yB6n",
  "password_version": 2,
  "rotated_at": "2026-11-10T15:02:10.123456+08:00"
}
```

新密码明文只在这一次响应中出现。

### 5.3 历史版本（不含明文）

`GET /api/instances/{id}/password-history/`

```json
[
  {"id": 5, "instance": 4, "account": "root", "version": 2, "reason": "manual", "rotated_at": "2026-11-10T15:02:10.123456+08:00"},
  {"id": 1, "instance": 4, "account": "root", "version": 1, "reason": "manual", "rotated_at": "2026-11-10T15:00:27.648749+08:00"}
]
```

`reason`：`scheduled`（定时轮换）/ `manual`（手工改密）/ `reset`（自动生成初始密码）。

---

## 6. 统计

### 6.1 明细（按 日期 × 部门 × 集群）

`GET /api/stats/daily/?date=2026-11-10&department=1&cluster=1`

也支持 `start` / `end` 区间。不传 `date` 时返回全部日期（按日期倒序）。

```json
{
  "count": 2,
  "next": null,
  "previous": null,
  "results": [
    {
      "id": 2, "stat_date": "2026-11-10",
      "department": 1, "department_name": "技术部", "department_code": "TECH",
      "cluster": 1, "cluster_name": "技术部 MySQL 生产集群", "cluster_code": "tech-mysql-prod",
      "total_instances": 2, "active_instances": 2, "online_instances": 1,
      "created_at": "2026-11-10T15:00:30.000000+08:00",
      "updated_at": "2026-11-10T15:00:30.000000+08:00"
    }
  ]
}
```

### 6.2 按部门汇总

`GET /api/stats/daily/summary/?date=2026-11-10`（不传日期则取最新有数据的一天）

```json
{
  "stat_date": "2026-11-10",
  "departments": [
    {"department_id": 2, "department_name": "财务部", "department_code": "FIN",
     "clusters": 1, "total_instances": 2, "active_instances": 2, "online_instances": 0},
    {"department_id": 1, "department_name": "技术部", "department_code": "TECH",
     "clusters": 2, "total_instances": 3, "active_instances": 3, "online_instances": 0}
  ],
  "total_instances": 5
}
```

### 6.3 手动触发

`POST /api/stats/daily/collect/`

```json
{"stat_date": "2026-11-10"}
```

有 broker 时（异步，202）：

```json
{"task_id": "0f6e...", "state": "PENDING", "stat_date": "2026-11-10"}
```

`CELERY_TASK_ALWAYS_EAGER=1` 时（同步，200）：

```json
{"state": "SUCCESS", "stat_date": "2026-11-10",
 "result": {"clusters": 3, "created": 3, "updated": 0, "removed": 0, "total_instances": 5}}
```

同一天重复触发是幂等的：`created=0, updated=N`，行数不变。

---

## 7. 请求耗时

### 7.1 明细

`GET /api/request-logs/?path=/api/instances/&method=DELETE&min_duration=50&since_id=100`

```json
{
  "count": 1,
  "next": null,
  "previous": null,
  "results": [
    {"id": 40, "method": "DELETE", "path": "/api/instances/8/", "status_code": 204,
     "duration_ms": 69.296, "user_repr": "", "client_ip": "127.0.0.1",
     "created_at": "2026-11-10T15:01:52.312808+08:00"}
  ]
}
```

### 7.2 分布

`GET /api/request-logs/stats/?limit=200`

```json
{"count": 40, "avg_ms": 106.164, "p50_ms": 7.774, "p95_ms": 1064.789, "max_ms": 1067.525}
```

---

## 8. 健康检查

`GET /api/health/`

```json
{"status": "ok", "database": true, "database_error": "",
 "encryption_key_configured": false, "time": "2026-11-10T15:02:30.123456+08:00"}
```

- 数据库不可用返回 **503** 且 `status=degraded`；
- `encryption_key_configured=false` 说明在用 `SECRET_KEY` 派生密钥，**生产环境必须配置**
  `INSTANCE_ENCRYPTION_KEYS`（`manage.py check --deploy` 会直接报错）。

---

## 9. 快速验证

```bash
# 起服务（无 Redis 也行）
CELERY_TASK_ALWAYS_EAGER=1 python manage.py runserver 8000

# 另开一个终端跑 32 项端到端断言
python scripts/e2e_check.py --base-url http://127.0.0.1:8000
```
