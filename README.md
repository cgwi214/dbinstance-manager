# 数据库实例管理系统（Django + DRF + Celery）

企业内部的数据库实例台账：管部门 / 集群 / 实例，管实例的账号密码（**加密存储 + 每 12 小时自动轮换**），
探测实例端口是否可达，每天 00:00 按部门 + 集群统计实例数量，并统计每个 HTTP 请求的耗时。

- 技术栈：Python 3.13 / Django 5.2 / Django REST Framework 3.16 / Celery 5.5 / cryptography(Fernet)
- 数据库：默认 SQLite（开箱即跑），生产切 PostgreSQL
- **不依赖 Redis 也能完整跑通全部功能**（用 `CELERY_TASK_ALWAYS_EAGER=1` + 管理命令），见「快速开始」
- **接口鉴权**：Token 认证 + 两档权限（登录用户只读、staff 才能写/看明文凭据），`API_AUTH_REQUIRED=1` 开启
- 已在真实 PostgreSQL 16.2 + Redis 8.10 上验证：128 个测试、真实 worker/beat、2 worker 并发轮换（详见 `docs/VERIFY.md`）

---

## 1. 快速开始（零外部依赖）

```bash
pip install -r requirements.txt

python manage.py migrate
python manage.py seed_demo            # 造演示数据：2 部门 / 3 集群 / 5 实例
python manage.py runserver 8000
```

打开 <http://127.0.0.1:8000/api/> 可以看到全部可浏览 API。

> 配置走环境变量，也支持 `.env`：`settings.py` 启动时会自动加载项目根目录的 `.env`
> （真实环境变量优先，不会被文件覆盖）。`.env` 已在 `.gitignore` 里，不会进版本库。
>
> ⚠️ 上面前四行命令**不需要** `.env`（默认走 SQLite、鉴权关闭）。如果你复制了 `.env.example`，
> 它默认把 `API_AUTH_REQUIRED` 设为 `1`，此时所有接口都会返回 401 —— 先发一个 token：
>
> ```bash
> python manage.py create_api_token --username ops
> curl -H "Authorization: Token <token>" http://127.0.0.1:8000/api/instances/
> ```

定时任务在本地有两种跑法：

```bash
# 方式一：进程内同步执行（不需要 broker）
python manage.py rotate_passwords          # 立即轮换所有启用实例的密码
python manage.py collect_stats             # 立即统计今天
python manage.py collect_stats --date 2026-11-09

# 方式二：打开 eager 模式后，HTTP 触发也同步执行
CELERY_TASK_ALWAYS_EAGER=1 python manage.py runserver 8000
curl -X POST http://127.0.0.1:8000/api/stats/daily/collect/ -H 'Content-Type: application/json' -d '{"stat_date":"2026-11-10"}'
```

## 2. 生产部署（Redis + worker + beat）

```bash
# 方式一：写进 .env（推荐本地/单机）
cp .env.example .env && ${EDITOR:-vi} .env

# 方式二：容器里用 -e / env_file 注入（优先级高于 .env）
export INSTANCE_ENCRYPTION_KEYS="<Fernet key>"      # 必须显式配置，见第 6 节
export CELERY_BROKER_URL="redis://redis:6379/0"
export CELERY_RESULT_BACKEND="redis://redis:6379/1"
export DB_ENGINE=postgres DB_NAME=dbinstance DB_USER=... DB_PASSWORD=... DB_HOST=...
export API_AUTH_REQUIRED=1                          # 生产必须开启鉴权

python manage.py check --deploy                     # 密钥/安全项检查
python manage.py migrate
python manage.py create_api_token --username ops            # 发一个 staff token（可写）
python manage.py create_api_token --username reader --no-staff   # 发一个只读 token

gunicorn config.wsgi:application -b 0.0.0.0:8000
celery -A config worker -l info                     # 执行任务
celery -A config beat -l info                       # 定时触发
```

> `worker` 和 `beat` 都要起：`beat` 只负责按点投递，`worker` 才真正执行。

### 2.1 鉴权

`API_AUTH_REQUIRED=1` 后：

| 身份 | 只读（GET） | 写（POST/PATCH/DELETE） | 看明文凭据 / 轮换密码 / 探测 |
|---|---|---|---|
| 匿名 | 401 | 401 | 401 |
| 普通用户（`--no-staff`） | 200 | **403** | **403** |
| staff | 200 | 200/201/204 | 200 |

```bash
curl -H "Authorization: Token <token>" http://127.0.0.1:8000/api/instances/
```

健康检查 `/api/health/` 例外：**不需要 token**，否则 k8s 探针没法用。

> 本地默认 `API_AUTH_REQUIRED=0`（开箱即用）；`.env.example` 里默认给了 1，照着复制就会开启。

## 3. 目录结构

```
dbinstance_manager/
├── config/
│   ├── settings.py        # 全部配置走环境变量；含 Celery beat 调度表
│   ├── celery.py          # Celery app（autodiscover_tasks）
│   └── urls.py
├── inventory/
│   ├── models.py          # 部门 / 集群 / 实例 / 密码历史 / 每日统计 / 请求日志
│   ├── crypto.py          # Fernet 加解密、随机密码生成
│   ├── fields.py          # EncryptedTextField：落库加密、读出解密
│   ├── services.py        # TCP 端口探测
│   ├── serializers.py     # 密码只进不出
│   ├── views.py           # CRUD + 探测 + 轮换 + 统计 + 请求耗时查询
│   ├── middleware.py      # 请求耗时中间件
│   ├── tasks.py           # 两个定时任务 + 按部门汇总
│   ├── checks.py          # 部署安全检查（check --deploy）
│   ├── management/commands/{seed_demo,rotate_passwords,collect_stats}.py
│   └── tests/             # 128 项断言
├── docs/API.md            # 接口文档（示例均为真实响应）
├── docs/DESIGN.md         # 设计说明与取舍
├── docs/VERIFY.md         # 验证报告（结论 / 证据 / 修掉的 bug / 未验证项）
├── scripts/
│   ├── e2e_check.py       # 端到端冒烟：对运行中的服务发真实请求（31 项断言，支持 --token）
│   ├── concurrency_probe.py  # 并发轮换压测：多 worker 抢同一批实例
│   └── beat_probe.py      # 验证 beat→broker→worker 链路（临时 20 秒调度）
├── config/settings_beat_probe.py  # 仅用于上面的验证，覆盖 beat 调度
├── .env.example           # 配置清单（复制成 .env 生效）
├── .gitattributes         # 换行符统一为 LF
└── requirements.txt
```

## 4. 数据模型

| 模型 | 关键字段 | 说明 |
|---|---|---|
| `Department` | `name` `code` `parent` `owner` `is_active` | 支持多级部门，`clean()` 检测自引用与环 |
| `Cluster` | `name` `code` `department` `engine` `environment` `vip` `port` | `(department, name)` 唯一；部门内同名集群不允许 |
| `DBInstance` | `name` `cluster` `role` `host` `port` `engine` `status` `account` `password_ciphertext` `password_version` `password_rotated_at` | `(cluster, name)`、`(host, port)` 唯一；密码字段透明加密 |
| `InstancePasswordHistory` | `instance` `account` `password_ciphertext` `version` `reason` | 只追加的审计流水，`(instance, version)` 唯一 |
| `InstanceDailyStat` | `stat_date` `department` `cluster` `total_instances` `active_instances` `online_instances` | `(stat_date, department, cluster)` 唯一，天然幂等 |
| `RequestLog` | `method` `path` `status_code` `duration_ms` `client_ip` | 中间件写入，供耗时统计查询 |

## 5. API 清单

| 方法 | 路径 | 说明 |
|---|---|---|
| GET/POST | `/api/departments/` | 列表（`search` `is_active` `parent` `ordering`）/ 新建 |
| GET/PUT/PATCH/DELETE | `/api/departments/{id}/` | 详情 / 更新 / 删除（被引用时 409） |
| GET/POST | `/api/clusters/` | 列表（`department` `engine` `environment` `is_active` `search`）/ 新建 |
| GET/PUT/PATCH/DELETE | `/api/clusters/{id}/` | 同上 |
| GET/POST | `/api/instances/` | 列表（`cluster` `department` `engine` `status` `role` `is_active` `search` `stale`）/ 新建 |
| GET/PUT/PATCH/DELETE | `/api/instances/{id}/` | 同上；`password` 只写 |
| POST | `/api/instances/{id}/probe/` | **需求 2**：探测该实例 TCP 端口，并回写探测结果 |
| POST | `/api/instances/{id}/rotate-password/` | 立即轮换，明文只返回这一次 |
| POST | `/api/instances/{id}/credential/` | 查看当前凭据明文，**必须填 reason**（写审计日志） |
| GET | `/api/instances/{id}/password-history/` | 密码版本历史（不含明文） |
| GET | `/api/stats/daily/` | 每日统计（`date` `start` `end` `department` `cluster`） |
| GET | `/api/stats/daily/summary/` | 按部门汇总某天 |
| POST | `/api/stats/daily/collect/` | 手动触发统计（生产 202+task_id；eager 模式同步返回结果） |
| GET | `/api/request-logs/` | 请求耗时明细（`path` `method` `min_duration` `since_id`） |
| GET | `/api/request-logs/stats/` | 最近 N 条的 avg/p50/p95/max |
| GET | `/api/health/` | 健康检查（数据库 + 密钥配置） |

响应头 `X-Request-Duration-Ms` 与 `Server-Timing` 携带每个请求的耗时。

## 6. 功能实现索引（逐条对照）

| 功能 | 实现位置 | 对外接口 / 触发方式 | 对应测试 |
|---|---|---|---|
| **多表模型 + 外键**（实例 / 集群 / 部门） | `models.py:29` `Department`（自关联 `parent`）、`:85` `Cluster`（→ 部门）、`:133` `DBInstance`（→ 集群）；配套 `:313` 密码历史（→ 实例）、`:342` 每日统计（→ 部门 + 集群）、`:373` 请求日志 | — | `test_models.py`（19 项） |
| **CRUD** | `views.py:96` / `:127` / `:157` 三个 `ModelViewSet`，序列化器 `serializers.py:17` / `:87` | `/api/departments/`、`/api/clusters/`、`/api/instances/` | `test_api.py`（40 项） |
| **主动探测 API**（TCP 端口） | `services.py:48` `tcp_probe()`：真实 `connect()`，区分可达 / 拒绝 / 超时 / DNS 失败；`views.py` 的 `probe` action 把结果回写 `last_probe_*` 与 `status` | `POST /api/instances/{id}/probe/`（调用方主动触发） | `test_probe.py`（7 项）+ `test_api.py` 探测用例 |
| **周期任务**（12 小时轮换） | `settings.py:204` `crontab(minute=0, hour="*/12")`（tz `Asia/Shanghai`）→ `tasks.py:48` `rotate_all_instance_passwords` | Celery beat 自动触发；也可 `POST /api/instances/{id}/rotate-password/` 或 `manage.py rotate_passwords` | `test_tasks.py:23` `test_rotation_runs_every_12_hours` + 冲突/重试用例 |
| **敏感字段加密** | `fields.py:31` `EncryptedTextField`：写库加密、读库解密，密文带 `enc:v1:` 前缀；`crypto.py:70` Fernet（多密钥可轮换）；密码历史表同样加密 | 库里只有密文；明文只能从 `credential`（需填原因）或轮换响应里拿一次 | `test_crypto.py`（11 项）落库密文断言 |
| **每日 00:00 多维度统计入库** | `settings.py:210` `crontab(minute=0, hour=0)` → `tasks.py:101` `collect_daily_instance_stats`；写 `InstanceDailyStat`，唯一键 `(stat_date, department, cluster)`，可重复执行 | beat 自动触发；`POST /api/stats/daily/collect/` 手工补跑 | `test_tasks.py:30` `test_stats_runs_daily_at_midnight` + 分组/幂等用例 |
| **Django 中间件** | `middleware.py:21` `RequestTimingMiddleware`，`settings.py:94` 注册在 `MIDDLEWARE[1]`（紧跟 SecurityMiddleware，覆盖全部后续中间件与视图） | 响应头 `X-Request-Duration-Ms` / `Server-Timing`；`GET /api/request-logs/`、`/stats/` | `test_middleware.py`（7 项） |

### 6.0 外键的三种删除路径（实测）

Django 把 `on_delete` 放在应用层实现，所以 PG 里这些外键的 `confdeltype` 都是 `NO ACTION`。
三种路径的实际行为：

| 路径 | 结果 |
|---|---|
| ORM 删有集群的部门 | `ProtectedError` → 接口层转 **409** |
| ORM 删实例 | 它的密码历史一起删掉（应用层 `CASCADE`），2 条 → 0 条 |
| 裸 SQL 删实例（绕过 ORM） | `IntegrityError: violates foreign key constraint` —— DB 层 `NO ACTION` 挡住了，**不会留下孤儿数据** |

### 6.1 三处需要说明的判断

1. **探测接口只保留实例级一个**（`POST /api/instances/{id}/probe/`）：探测对象就是"某个实例"，
   探测结果会回写到 `last_probe_*` 与 `status`。独立地址探测（`/api/probe/`）已下线。
2. **轮换覆盖所有实例（含已停用的）**：停用只是"不再对外服务"，账号和凭据仍然有效，
   不轮换就等于长期使用静态口令。要跳过停用实例，在 `tasks.py` 里加回 `.filter(is_active=True)`。
3. **00:00 统计的是"当天日初快照"**（默认 `timezone.localdate()`）：00:00 跑的时候当天数据尚未产生，
   这一行代表的是"进入这一天时的存量"。如果更希望标成"昨天结束时的存量"，把默认日期改成
   `localdate() - 1 天` 即可，表结构不用动。

## 7. 关键设计决策（细节见 `docs/DESIGN.md`）

1. **加密放在字段层**，不在业务代码里：`EncryptedTextField` 在写库时加密、读库时解密。Python 侧永远是明文，
   数据库里永远是 `enc:v1:...` 密文——业务代码不可能“忘记加密”。
2. **密码只进不出**：序列化器里 `password` 是 `write_only`；列表/详情接口不返回任何密码字段。
   需要明文只有两个入口：`rotate-password`（返回一次新密码）和 `credential`（必须填原因，写 WARNING 审计日志）。
3. **轮换用乐观锁**：`UPDATE ... WHERE password_version = <旧值>`，配合历史表 `(instance, version)` 唯一约束，
   多 worker 并发轮换不会写出两条同版本历史；冲突时任务重试（默认 3 次）。
4. **统计任务幂等**：`update_or_create` + 唯一约束，同一天重复跑只更新不重复插入；没有实例的集群也留一条 0 记录。
5. **探测只做 TCP 握手**，区分「可达 / 连接被拒 / 超时 / DNS 失败」四种结果，超时被夹在 0.1~30s，避免请求被长期占用。
6. **中间件不吞异常**：耗时在 `finally` 里统计，5xx 同样记录；写库失败只记日志，绝不影响业务请求。

## 8. 测试

```bash
python manage.py test inventory          # 128 个单测/接口测试
python manage.py check --deploy          # 未配置密钥时会报 inventory.E001

# 端到端：对运行中的服务发真实 HTTP 请求（31 项断言，含真实 TCP 探测与统计交叉核对）
CELERY_TASK_ALWAYS_EAGER=1 python manage.py runserver 8000
python scripts/e2e_check.py --base-url http://127.0.0.1:8000
```

真实 worker / beat（需要 Redis）：

```bash
celery -A config worker -l info --pool=solo    # Windows 用 solo；Linux 可用默认 prefork
celery -A config beat   -l info                # 00:00 与 12:00 触发
```

接口请求/响应示例见 `docs/API.md`。

### 8.1 依赖审计（`test_dependencies.py`）

依赖只有 6 个，全部来自 PyPI 官方包，且由测试守着不被偷偷扩大：

| 发行包 | 用途 | 谁在用 |
|---|---|---|
| `Django` | Web 框架 | 全项目 |
| `djangorestframework` | REST 接口层 | `serializers.py` / `views.py` / `urls.py` |
| `celery` | 定时任务 | `config/celery.py` / `tasks.py` |
| `cryptography` | 密码字段加密（Fernet） | `crypto.py` / `tests/test_crypto.py` |
| `psycopg[binary]` | PostgreSQL 驱动（不直接 import，Django 用） | 运行时 |
| `redis` | Celery broker 客户端（不直接 import，Celery 用） | 运行时 |

`test_dependencies.py` 做 5 项静态守卫：**不允许动态导入**（`importlib`/`__import__` 会绕过审计）、
每个第三方 import 必须在映射表里登记、登记的包必须在 `requirements.txt` 里、`requirements.txt` 里不能有没人用的包、
每个模块必须能真正加载且指向非空文件（防"空壳包"）。

验证方式：**新建一个只装 `requirements.txt` 的干净虚拟环境**跑全量测试（128/128 通过），
证明依赖清单完整、不依赖任何"碰巧装在共享环境里"的包。

覆盖：加密往返与密钥轮换、字段落库确为密文、密码版本/历史、部门环检测、唯一约束、三个资源的 CRUD 与过滤、
探测（真实监听 socket）、凭据/轮换/历史接口、统计任务分组与幂等、beat 调度表达式、中间件响应头与落库。
详见 `docs/VERIFY.md`。

## 9. 已知边界

- 部门 / 集群 / 实例的删除使用 `PROTECT`：有下游数据时返回 **409**，请改用 `is_active=false` 下线。
- 鉴权是**自建 Token**（`create_api_token` 发），不是公司 SSO；接入 SSO 时替换 `DEFAULT_AUTHENTICATION_CLASSES` 即可，
  权限策略（读/写两档）不用动。生产还应叠加来源 IP 白名单。
- `RequestLog` 每请求写一行，高 QPS 下建议关闭 `REQUEST_TIMING_PERSIST` 或降低 `REQUEST_TIMING_SAMPLE_RATE`。
- 密码轮换只更新本系统的记录，不会去改数据库实例上的真实密码（需要各引擎的 `ALTER USER` 适配器，未在本题范围内）。
- 本次验证用的 PostgreSQL / Redis 都是**弱口令**（Redis 还开了 `requirepass` 但口令极简），
  只适合内网开发环境；上线前必须换成强口令并限制来源 IP。
