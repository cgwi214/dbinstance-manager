# 验证报告

本文件是唯一的验证记录，全部结论都在 2026-11-11 当天、在真实 PostgreSQL + Redis 上重新跑过。
环境：Windows / Python 3.13.14 / Django 5.2.19 / DRF 3.16.1 / Celery 5.5.3 / cryptography 50.0.1。

| 组件 | 版本 | 地址 | 用途 |
|---|---|---|---|
| PostgreSQL | 16.2 (Debian) | `<PG_HOST>:5432`，库 `dbinstance` | 主数据库 |
| Redis | 8.10.3 (standalone) | `<REDIS_HOST>:6379` | Celery broker(db0) / result backend(db1) |
| SQLite | 3.x | `var/db.sqlite3` | 零依赖本地模式的回退 |

配置走项目根目录 `.env`（`settings.py` 自动加载，真实环境变量优先）：
`DB_ENGINE=postgres`、`CELERY_*` 指向上面两个服务、`INSTANCE_ENCRYPTION_KEYS` 显式配置。
本地 `.env` 里 `API_AUTH_REQUIRED` 未开（开箱即用），鉴权路径由测试和单独一次开启验证覆盖。

---

## 1. 结论一览

| # | 验证项 | 方法 | 结果 |
|---|---|---|---|
| 1 | 静态检查 | `manage.py check` + `pyflakes` | 0 issue / 0 warning |
| 2 | 单元与接口测试 | `manage.py test inventory`（PostgreSQL） | **128 项全通过**（8.4s） |
| 3 | 数据库可移植性 | 同一套测试在 PostgreSQL 与 SQLite 各跑一遍 | 两边都 128/128 |
| 4 | 鉴权与越权 | 真实 HTTP 打 9 种身份/动作组合 | 401 / 403 / 200 全部符合预期 |
| 5 | 端到端 | `scripts/e2e_check.py --token ...`（异步 + 鉴权） | **32 项断言全通过** |
| 6 | 异步链路 | HTTP → Redis → worker → PostgreSQL | 202 → 落库 → 结果回写，全通 |
| 7 | beat 到点派发 | 真实 beat 进程（临时 20 秒调度） | 连续两次 `Sending due task` 被消费 |
| 8 | 并发轮换 | 2 worker × 4 任务抢 5 个实例 | 版本连续、无重复、8 次冲突自愈 |
| 9 | 并发统计 | 8 个任务写同一日期 | 0 失败、数字一致、无重复分组 |
| 10 | 依赖完整性 | 只装 `requirements.txt` 的干净 venv 跑全量 | 128/128 通过 |
| 11 | 首次交付 | 无 `.env`、无 `var/` 的副本按 README 跑 | 开箱可用 |
| 12 | 部署检查 | `manage.py check --deploy` | 未配密钥报 `inventory.E001`，配了不报 |

---

## 2. 测试套件（128 项）

```bash
$ python manage.py test inventory
Ran 128 tests in 8.390s
OK
```

| 文件 | 数量 | 覆盖重点 |
|---|---|---|
| `test_auth.py` | 22 | **鉴权与越权**：匿名 401、错 token 401、读写矩阵、明文凭据越权、健康检查放行、token 命令 |
| `test_api.py` | 40 | 三个资源 CRUD、过滤搜索分页、密码不外泄、自动生成密码、409/400 边界、实例级探测、凭据、轮换、统计、请求日志 |
| `test_models.py` | 19 | 部门层级与环、密码版本/历史、乐观锁冲突、过期判定、唯一约束、探测回写状态机、PROTECT |
| `test_tasks.py` | 17 | beat 表达式、任务注册、**轮换覆盖含停用实例**、冲突重试、重试耗尽、单点失败不中断、统计分组与幂等 |
| `test_crypto.py` | 11 | 加解密往返、随机 IV、坏密文不 500、**坏密文不被 save 覆盖**、密码生成规则、多密钥轮换 |
| `test_probe.py` | 7 | 真实 socket 可达、端口关闭、DNS 失败、空 host、非法端口、超时夹取、**timeout 是总预算** |
| `test_middleware.py` | 7 | 响应头与落库一致、404 也记录、静态前缀跳过、可关闭落库、自定义头名、query string、X-Forwarded-For |
| `test_dependencies.py` | 5 | 禁动态导入、第三方 import 必须登记、requirements 无缺无冗余、模块能真加载且非空 |

断言不是"接口返回 200"，而是可证伪的不变量，例如：

- 密码列在库里**只能是密文**（原生 SQL 读出比对 `enc:v1:` 前缀）；
- 列表/详情响应里**不存在** `password` / `password_ciphertext`；
- 陈旧对象轮换必须抛 `PasswordRotationConflict`，且**不推进版本、不写历史**；
- 普通用户访问 `credential` 必须 403，且响应里**没有** password 字段。

---

## 3. 鉴权与越权

### 3.1 权限模型

| 身份 | 只读（GET） | 写（POST/PATCH/DELETE） | 明文凭据 / 轮换 / 探测 |
|---|---|---|---|
| 匿名 | 401 | 401 | 401 |
| 普通用户（`--no-staff`） | 200 | **403** | **403** |
| staff | 200 | 200/201/204 | 200 |

`API_AUTH_REQUIRED=1` 生效；`/api/health/` 显式放行（否则 k8s 探针被 401 判死）。

### 3.2 真实 HTTP 验证（PostgreSQL 环境）

起服务：`API_AUTH_REQUIRED=1 manage.py runserver`，用 `create_api_token` 发 staff 与普通用户两个 token：

```
[OK] 匿名读实例          401  (期望 401)
[OK] 匿名读健康检查      200  (期望 200)
[OK] 错误 token          401  (期望 401)
[OK] reader 读实例       200  (期望 200)
[OK] reader 建部门       403  (期望 403)
[OK] ops 建部门          201  (期望 201)
[OK] reader 看明文密码   403  (期望 403)
[OK] ops 看明文密码      200  (期望 200)
[OK] reader 轮换密码     403  (期望 403)
鉴权矩阵: 全部符合预期
```

最后三行是越权的核心：**同一个接口、同一个实例，普通用户拿不到明文也改不了密码，staff 可以**。

### 3.3 默认关闭，生产必须打开

`API_AUTH_REQUIRED` 默认 `0`，保证"装好就能跑"。这样设计的原因很实际：
如果默认开启，`.env` 一加载 `manage.py test` 就全 401，一百多个测试一起红。
权限判定放在**请求时**读 settings，测试用 `override_settings(API_AUTH_REQUIRED=True)` 精确打开，
两个模式都能测到，也不需要另建一套 settings 模块。

---

## 4. 端到端（异步 + 鉴权，32 项断言）

```bash
API_AUTH_REQUIRED=1 python manage.py runserver 8000
python scripts/e2e_check.py --token <staff-token> --cleanup
```

```
[PASS] 服务与数据库可用 — status=200
[PASS] 响应头带请求耗时 — 2.67 ms
[PASS] 创建部门 / 集群 / 实例 — status=201
[PASS] 响应不含任何密码字段
[PASS] 删除仍被引用的集群返回 409
[PASS] 可达端口探测为 reachable — 1.064 ms
[PASS] 关闭端口探测为 unreachable — 连接被拒绝（端口未监听）
[PASS] 独立探测接口已下线（只保留实例级） — status=404
[PASS] 查看凭据缺少 reason 被拒绝 — status=400
[PASS] 轮换后密码变化 — 版本=2
[PASS] 密码历史记录两个版本 — [1, 2]
[PASS] worker 已异步落库 — 3 行
[PASS] 统计数字与实例接口一致 — 全部一致
[PASS] 请求耗时统计有数据 — {"count": 109, "p50_ms": 16.864, "p95_ms": 155.242}

共 32 项断言，通过 32 项，失败 0 项
```

统计那一步是**交叉核对**：把统计表里每个部门的总数与 `GET /api/instances/?department=X`
的 `count` 逐个比对，而不是只看"有没有数据"。

---

## 5. 异步链路与定时任务（真实 Redis）

### 5.1 HTTP → broker → worker → PostgreSQL

```
匿名访问        : 401 (期望 401)
POST collect    : 202 STARTED task_id: 0e4e3fd9
worker 已落库   : 3 行
汇总            : 200 部门数 2 总实例 5
```

### 5.2 beat 到点真的会派发

真实 beat 落盘的调度文件里读出来是 `crontab: 0 0 * * *` 与 `0 */12 * * *`（tz Asia/Shanghai）。
00:00 等不起，用 `config/settings_beat_probe.py` 把调度临时改成 20 秒（其余配置不变）：

```
10:51:22  beat: Starting...
10:51:42  Scheduler: Sending due task probe-every-20s (inventory.tasks.collect_daily_instance_stats)
10:52:02  Scheduler: Sending due task probe-every-20s (inventory.tasks.collect_daily_instance_stats)
```

两个 worker 各消费 1 次。**beat → Redis → worker → PostgreSQL 这条链路是通的**，不是"配置看起来对"。

> 踩过的坑：在脚本里 `app.conf.beat_schedule = {...}` 再 `app.start(["beat"])` 是无效的，
> `app.start()` 会重新读取配置把改动覆盖掉。要改 beat 调度必须走 settings 模块。

### 5.3 并发轮换（2 worker / 4 任务 / 5 实例）

```bash
celery -A config worker -l info --pool=solo -n w1@%h
celery -A config worker -l info --pool=solo -n w2@%h
python scripts/concurrency_probe.py --tasks 4
```

```
参与实例 5 个，初始版本 [10]，投递 4 个任务
任务1 a114d428 -> total=5 rotated=5 failed=0
任务2 93d55e9a -> total=5 rotated=5 failed=0
任务3 6c2832f8 -> total=5 rotated=5 failed=0
任务4 56091e95 -> total=5 rotated=5 failed=0

PASS: 5 个实例 × 4 个任务，版本连续、历史无重复、最新历史密码与当前密码一致
```

worker 日志里「版本冲突，重试」共 8 次（w1 2 次 / w2 6 次）——冲突确实发生了，且全部重试自愈。

逐实例核对的不变量：`password_version == 初始版本 + 成功任务数`、
历史版本严格等于 `1..password_version`、最新一条历史的密码等于当前密码。

### 5.4 并发统计（8 个任务写同一日期）

`update_or_create` 在并发下是经典的"两个事务都没查到 → 都去 INSERT → 撞唯一约束"场景：

```
投递 8 个并发统计任务
失败任务数: 0
落库行数: 3 （集群数应为 3 ）
数字与实际不符的行: 无，全部一致
重复分组: 无
```

并发安全。原因是 Django 5.x 的 `update_or_create` 内部走 `select_for_update().get_or_create()`：
已存在的行被行锁串行化，行不存在时插入竞争由 `get_or_create` 捕获 `IntegrityError` 后重查兜住，
再加 `(stat_date, department, cluster)` 唯一约束兜底。

---

## 6. 首次交付验收（模拟 git clone）

把项目复制到干净目录（**不含 `.env`、不含 `var/`**），用只装 `requirements.txt` 的隔离 venv，
完全按 README 走：

```
manage.py check            → 0 issues
manage.py migrate          → OK（sqlite，自动建 var/）
manage.py seed_demo        → 2 部门 / 3 集群 / 5 实例
manage.py collect_stats    → 3 个集群落库
manage.py rotate_passwords → 5/5 轮换成功
runserver + 打接口          → health 200 / departments 200（2 个）/ instances 200（5 个，无密码泄露）
                             响应头 X-Request-Duration-Ms = 16.62
```

结论：零配置开箱可用。这一步也抓到过一个静默 bug：`?page_size=` 被 DRF 默认分页忽略
（不报错、默默按 20 返回），已修并补测试。

---

## 7. 依赖与供应链审计

### 7.1 依赖清单（6 个，全部来自 PyPI）

| 发行包 | 版本 | 用途 | 代码里怎么用 |
|---|---|---|---|
| `Django` | 5.2.19 | Web 框架 | 全项目 |
| `djangorestframework` | 3.16.1 | REST 接口层 | `serializers.py` `views.py` `urls.py` |
| `celery` | 5.5.3 | 定时任务 | `config/celery.py` `tasks.py` |
| `cryptography` | 50.0.1 | 密码字段加密（Fernet） | `crypto.py` |
| `psycopg[binary]` | 3.3.8 | PostgreSQL 驱动 | 运行时（Django 连接层，代码不直接 import） |
| `redis` | 8.1.0 | Celery broker 客户端 | 运行时（代码不直接 import） |

AST 全量扫描：代码里直接 import 的第三方顶层模块只有 4 个
（`django` / `rest_framework` / `celery` / `cryptography`）。

### 7.2 投毒面检查

| 检查项 | 结果 |
|---|---|
| 发行包来源 | 7 个 dist 均无 `direct_url.json` → 都来自 PyPI，不是 git/URL 直装 |
| 元数据一致性 | Django Software Foundation / Tom Christie / Ask Solem / Python Cryptographic Authority |
| 标准库冒充 | `site-packages` 顶层无与标准库同名的模块 |
| 代码注入点 | 无 `.pth`、无 `sitecustomize.py`、无 `usercustomize.py` |
| 动态导入 | 无 `importlib.import_module` / `__import__`（有测试守着） |
| 危险执行面 | 无 `eval` / `exec` / `compile` / `os.system` / `subprocess` / `pickle.loads` |
| pip 源 | 无 `pip.conf` 覆盖、无 `PIP_INDEX_URL`、项目内无 `--index-url` / `git+` |

### 7.3 干净环境验证

共享 venv 里混着别的任务装的包（pymupdf、openpyxl、pycryptodome…），不能用来证明依赖完整性。
所以新建隔离环境：

```
$ python -m venv envs/dbinstance_clean
$ envs/dbinstance_clean/Scripts/pip install -r requirements.txt
   -> 25 个包（6 个直接依赖 + 19 个传递依赖）

$ (clean) manage.py test inventory
Ran 128 tests ... OK

# 隔离性：共享环境里的包在干净环境里不可见
pymupdf / openpyxl / pycryptodome / requests / pyinstxtractor -> 均不可见
```

---

## 8. 代码审查中发现并修复的问题

| # | 问题 | 影响 | 修复 |
|---|---|---|---|
| 1 | 解密失败时字段返回空串，之后任意全量 `save()` 把密文列覆盖成空 | **密码永久丢失**：密钥配错一次就解不回来 | `UndecryptableValue`：对外空串，内部保留密文，写回原样落库 |
| 2 | 部门环只能靠 `Model.clean()` 拦，而 DRF 不调 `full_clean()` | API 可造出 `A→B→A`，`full_path` 套娃 20 层 | 抽出 `would_create_cycle()`，serializer 显式调用 |
| 3 | 探测成功会把 `maintenance` 改回 `online` | 人工维护标记被自动覆盖 | 维护中一律不动，只在 online/offline/unknown 之间流转 |
| 4 | 探测的 `timeout` 实际是"每个地址"预算 | 多地址域名最坏耗时 = 地址数 × timeout | 改成总预算（deadline + 剩余时间） |
| 5 | `check_password` 直接用 `compare_digest` 比较 str | 含非 ASCII 的密码抛 `TypeError`（500） | 统一转 bytes 比较 |
| 6 | 中间件在 `finally` 里调用 `_handle`，其自身异常会盖掉业务异常 | 真实错误被"统计失败"顶替 | 整段包 try/except，只记日志 |
| 7 | 任务返回 `finished_at` 用 UTC，API 其它时间 `+08:00` | 同一系统两种时区口径 | 统一 `timezone.localtime()` |
| 8 | 部署检查只匹配 `localhost`，漏 `127.0.0.1` | 本机 broker 逃过告警 | 三个本机地址都匹配 |
| 9 | `?page_size=` 被 DRF 默认分页静默忽略 | 调用方以为参数生效了 | 新增 `StandardPagination`，上限 200 |
| 10 | 密码在模型内存里存的是密文 | `check_password` 恒 False、`save()` 二次加密 | 模型层只放明文，加密只在 `get_prep_value` |
| 11 | `task.apply()` 需要 result backend | 本地无 Redis 时管理命令直接崩 | 改用 `task.run()`（`run_sync`） |
| 12 | eager 模式下 `collect` 仍走 `.delay()` | 无 broker 时接口 500 | 按 `CELERY_TASK_ALWAYS_EAGER` 分支同步执行 |
| 13 | `DEBUG=False` 就抛密钥异常 | Django 测试运行器下全部测试崩 | 派生兜底 + `check --deploy` 强校验 |
| 14 | `PROTECT` 外键删除抛 `ProtectedError` | DRF 不转换，删除接口 500 | 统一转 409 |
| 15 | 手工 `isoformat()` 输出 UTC | 同一响应里 `+08:00` 与 `+00:00` 混用 | 统一 `_iso()` |
| 16 | `.values("department__name")` 直接返回 | 响应里出现 ORM 风格键名 | `values(department_name=F(...))` 别名 |
| 17 | PostgreSQL 连接没有 `connect_timeout` | 库不可达时请求**一直挂着**（不报错也不响应） | `OPTIONS: {"connect_timeout": 5}`，5 秒内快速失败 |

---

## 9. 需求方确认后的行为变更

| 变更 | 原行为 | 现行为 |
|---|---|---|
| 探测接口 | 实例级 + 独立地址两个 | **只留实例级** `POST /api/instances/{id}/probe/`，`/api/probe/` 返回 404 |
| 轮换范围 | 只轮换 `is_active=True` | **所有实例**（含停用）——停用≠凭据失效 |
| 统计日期口径 | 当天 | 保持当天（00:00 记"日初快照"） |
| 鉴权 | 无（`AllowAny`） | Token 认证 + 两档权限，`API_AUTH_REQUIRED` 开关 |

---

## 10. 未验证项

| 项 | 原因 | 自检方法 |
|---|---|---|
| 真实数据库改密 | 需各引擎 `ALTER USER` 适配器，本期未实现 | — |
| 公司 SSO 对接 | 当前是自建 Token | 替换 `DEFAULT_AUTHENTICATION_CLASSES`，权限类不用动 |
| beat 连续运行 24h | 只验证了"到点会派发"，没跑满一天 | 跑满一天后核对统计与轮换历史的时间点分布 |
| 高 QPS 下中间件开销 | 未做压测 | 用 ab/wrk 打 1000 QPS，对比 `REQUEST_TIMING_PERSIST=0/1` 的 P99 |

---

## 11. 复跑全部验证

```bash
# 0) 干净环境（只装 requirements.txt）
python -m venv /tmp/clean && /tmp/clean/bin/pip install -r requirements.txt

# 1) 静态 + 单测（128 项，PostgreSQL）
python manage.py check
python -m pyflakes inventory config/settings.py manage.py scripts/*.py
python manage.py test inventory

# 2) 端到端（开鉴权，32 项断言）
export API_AUTH_REQUIRED=1
python manage.py create_api_token --username ops
python manage.py runserver 8000 &
python scripts/e2e_check.py --token <上一步输出的 token>

# 3) 异步链路 + beat（需要 Redis）
celery -A config worker -l info --pool=solo
DJANGO_SETTINGS_MODULE=config.settings_beat_probe celery -A config beat -l info -s var/beat-schedule-probe

# 4) 并发轮换（需要两个 worker）
celery -A config worker -l info --pool=solo -n w1@%h
celery -A config worker -l info --pool=solo -n w2@%h
python scripts/concurrency_probe.py --tasks 4
```
