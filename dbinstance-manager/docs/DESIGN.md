# 设计说明

本文记录每个需求的实现思路、关键取舍，以及「为什么不用另一种看起来更简单的做法」。

---

## 0. 总体分层

```
HTTP 层     views.py（ViewSet + action）        ← 参数校验、权限、序列化
            serializers.py
            middleware.py                        ← 请求耗时
            ↓
领域层      models.py（不变量 + 密码状态机）      ← 密码版本、唯一约束、探测回写
            services.py（TCP 探测，无 Django 依赖）
            ↓
基础设施    fields.py + crypto.py（加密）         ← 落库加密 / 读出解密
            tasks.py（Celery 任务，可被命令行同步调用）
            ↓
存储        SQLite / PostgreSQL + Celery broker
```

把「能脱离模型算的逻辑」放在本地（探测、统计聚合、密码生成、加解密），
只有定时触发这一层依赖 Celery —— 这样绝大部分行为可以在没有 broker 的环境里被测试覆盖。

---

## 1. 模型设计

### 1.1 层级与约束

```
Department ──< Cluster ──< DBInstance
     │              │
     └──< InstanceDailyStat >──┘        （统计维度直接落到 dept + cluster）
```

刻意加的唯一约束，每一条都对应一个真实事故场景：

| 约束 | 防止什么 |
|---|---|
| `Cluster (department, name)` | 同一部门下出现两个“订单库”，运维分不清 |
| `DBInstance (cluster, name)` | 集群内实例重名，监控指标串号 |
| `DBInstance (host, port)` | 同一 `host:port` 被登记成两条，轮换密码时互相覆盖 |
| `InstancePasswordHistory (instance, version)` | 并发轮换写出两条同版本历史，审计无法判断哪条生效 |
| `InstanceDailyStat (stat_date, department, cluster)` | 任务重跑导致同一天重复统计，报表翻倍 |

### 1.2 为什么部门是自关联而不是字符串

`parent` 自关联 + `clean()` 里沿父链走一遍检测环。数据库层不做环检测（成本高），
应用层拦截足够——部门维护是低频操作，且入口只有 Admin 与 API。

### 1.3 删除策略：PROTECT + 409

删部门/集群时如果还有下游数据，`ProtectedError` 会让 DRF 直接 500。这里统一在
`ProtectedDestroyMixin.perform_destroy` 里转成 **409 + 提示关联对象类型**。

真实运维语义是「下线而不是删除」，所以推荐 `is_active=false`；硬删除只用于刚建错的记录。
代价是：统计表对集群是 `PROTECT`，一旦产生过统计，该集群就无法硬删——这是有意的，
历史报表不该因为删掉一个集群而出现空洞。

---

## 2. 密码存储与轮换（需求 3）

### 2.1 加密放在字段层

```python
password_ciphertext = EncryptedTextField(...)   # 写库加密 / 读库解密
```

- **Python 侧永远是明文**，数据库里永远是 `enc:v1:<token>`。
- 好处：`instance.password`、`check_password()`、序列化器都不需要关心密文格式，
  也就不可能出现「某个新写的接口忘了加密」。
- 代价：任何 `save()` 都会重新加密（Fernet 用随机 IV，密文每次都不同），
  所以更新其它字段时用 `save(update_fields=[...])`，避免无意义地重写密码列。
- **解密失败不会毁数据**：密钥配错时读出来是空串，但如果直接把它写回，密文就被抹掉了。
  字段为此返回 `UndecryptableValue`——对外表现为空串，内部保留原始密文，
  写回时原样落库。密钥改回来之后数据还在。

### 2.2 为什么用 Fernet（而不是自己拼 AES）

Fernet = AES-128-CBC + HMAC-SHA256 + 随机 IV + 时间戳，**自带完整性校验**：
密文被篡改会抛 `InvalidToken`，不会解出一段垃圾明文。自己拼 AES-CBC 最容易漏掉的就是认证。

`enc:v1:` 前缀是为了将来换算法时能识别老数据；解密失败**不抛异常给接口**，
而是记 ERROR 日志并返回空串——单条坏数据不该让整个列表接口 500（有测试覆盖）。

### 2.3 密钥管理

- `INSTANCE_ENCRYPTION_KEYS` 支持逗号分隔多密钥，用 `MultiFernet`：
  **第一个加密，其余只用于解密**，于是密钥轮换不需要停机、也不需要批量重写数据。
- 未配置时从 `SECRET_KEY` 派生，保证「装好就能跑」；
  但 `manage.py check --deploy` 会把它报成 **inventory.E001 错误**（见 `checks.py`），
  因为 `SECRET_KEY` 一变，历史密文就永久解不开。
- 之所以用 `deploy=True` 的检查而不是普通检查：本地开发和单元测试需要零配置可跑，
  而部署必须严格——两件事不能用一个开关。

### 2.4 随机密码

`secrets.SystemRandom` + 24 位，字符集去掉 `l/I/O/0/1`（运维要手抄），
强制四类字符各至少一个。`secrets` 而不是 `random`：后者是可预测的梅森旋转。

### 2.4.1 轮换范围：所有实例，含已停用的

`rotate_all_instance_passwords` 取的是 `DBInstance.objects.all()`，**不过滤 `is_active`**。

理由：`is_active=False` 只表示"不再对外提供服务"，账号本身还活着、凭据仍然有效。
跳过它们等于让一批凭据长期不换——恰恰是最容易被忽略的风险点。
真要跳过，在 `tasks.py` 里加回 `.filter(is_active=True)` 即可，一行。

### 2.5 轮换的并发正确性

每 12 小时一批 worker 可能同时跑（滚动发布、手动补跑），所以：

```python
UPDATE dbinstance SET password_ciphertext=..., password_version=old+1
 WHERE id=? AND password_version=old          -- 乐观锁
```

- 影响行数为 0 ⇒ 说明别人已经推进过版本，抛 `PasswordRotationConflict`，任务重新加载后重试（默认 3 次）。
- 历史表 `(instance, version)` 唯一约束是最后一道保险：即使乐观锁被绕过，也不会写出两条同版本记录。
- 没有用 `SELECT ... FOR UPDATE`：SQLite 不完全支持行锁，乐观锁在两种数据库上行为一致。
- **实测**（PostgreSQL 16.2 + 2 个 worker + 4 个任务抢 5 个实例）：版本号严格连续、
  历史无重复、最新历史密码与当前密码一致，期间发生 16 次冲突重试且全部自愈。
  复现脚本 `scripts/concurrency_probe.py`。

### 2.6 轮换任务的返回

**绝不返回明文密码**（有测试断言 `str(summary)` 里不含任何实例密码）。
需要明文只有两个受控入口：`rotate-password`（单实例，返回一次）和 `credential`（必须填原因，写审计日志）。
真实系统里这两个入口还应接审批流——不在本题范围内，已在 README「已知边界」中标注。

---

## 3. 定时调度

`settings.CELERY_BEAT_SCHEDULE`（`0 */12 * * *` 与 `0 0 * * *`，时区 `Asia/Shanghai`）：

- 用**代码里的静态调度**而不是 `django-celery-beat` 的库表调度：调度规则能进 Git、能 review。
  如果运维需要不发布就改时间，再引入 `django-celery-beat` 是平滑的。
- 每条都设了 `expires`（6 小时）：避免 worker 堆积时，凌晨的任务在中午才被执行——密码轮换补跑没有意义，
  统计任务有幂等保护但也没必要补。
- 时区显式设成 `Asia/Shanghai` 且 `USE_TZ=True`：`crontab` 按 `CELERY_TIMEZONE` 解释，
  不设置的话在 UTC 机器上「每天 00:00」会变成北京时间 08:00。

### 3.1 为什么统计的默认日期是「今天」

任务在 00:00 触发，此刻按「今天」统计得到的正是**日初存量**（= 昨天结束时的状态），
把它记在今天这一行，语义是「今日日初实例数」，重跑同一天也只更新同一行。

若业务口径要的是「昨天全天的存量」，把 `tasks.py` 里 `_parse_date(None)` 的默认值改成
`timezone.localdate() - timedelta(days=1)` 即可，其余逻辑不变（`--date` 参数始终显式优先）。

---

## 4. 每日统计（需求 4）

```python
Cluster.objects.annotate(
    total=Count("instances", distinct=True),
    active=Count("instances", filter=Q(instances__is_active=True), distinct=True),
    online=Count("instances", filter=Q(instances__status="online"), distinct=True),
)
```

- **从 `Cluster` 出发而不是从 `DBInstance` 出发**：这样没有实例的集群也会得到一条 `total=0` 的记录，
  报表能看出「哪些集群是空的」，而不是静默缺失。
- `distinct=True` 必需：多个带 `filter` 的聚合会复用同一个 JOIN，不去重会互相放大计数。
- **幂等**：`update_or_create` 命中 `(stat_date, department, cluster)` 唯一约束；
  同一日期重复执行 `created=0, updated=N`，行数不变（有测试断言）。
- 同一事务内清理该日期已不存在集群的残留行，避免集群调整后留下脏数据。
- 只统计「存量快照」，不做增量差分——快照天然可重算，差分一旦错位无法修复。

---

## 5. TCP 端口探测（需求 2）

对外只有一个探测接口 `POST /api/instances/{id}/probe/`（探测对象就是"某个实例"），
探测结果回写实例。早期版本另有不落库的 `/api/probe/`，已下线——少一个接口就少一处攻击面
（任意地址探测等于给了内网扫描能力）。

`services.tcp_probe()` 只做 TCP 握手，不做协议层认证，因此能区分四种失败：

| 情况 | 结果 |
|---|---|
| 端口监听 | `reachable=True` + `latency_ms` + `resolved_ip` |
| 端口未监听 | `ConnectionRefusedError` → 「连接被拒绝」 |
| 被防火墙丢包 | `TimeoutError` → 「连接超时」 |
| 域名解析失败 | `gaierror` → 「DNS 解析失败」 |

- 超时被夹在 `0.1 ~ 30s`：接口直接收用户传的 timeout，不夹住就能构造一个挂 1 小时的请求。
- `timeout` 是**整次探测的总预算**，不是单个地址的预算。这里手写 `getaddrinfo` + `connect`
  循环，每个地址只用"剩余时间"做超时——否则一个解析出 3 个地址的域名最坏要等 3×timeout。
- **同步阻塞**是刻意的：单次探测的阻塞上限由 timeout 保证，接口语义简单（调用方要立刻知道结果）。
  批量巡检场景应改为 Celery 任务 + 异步回写，这里保留了 `record_probe()` 作为复用点。
- 探测结果回写 `last_probe_*`，状态机很克制：**`maintenance` 一律不动**（人工状态不该被自动探测覆盖），
  其余情况只在 `unknown/offline ↔ online` 之间流转（有测试覆盖两个方向）。

---

## 6. 请求耗时中间件（需求 5）

```python
started = time.perf_counter()
try:
    response = self.get_response(request)
    return response
finally:
    duration_ms = (time.perf_counter() - started) * 1000
    ...
```

几个细节：

- **`perf_counter` 而不是 `time.time`**：单调时钟，不受 NTP 校时影响（`time.time` 回拨会算出负耗时）。
- **`finally` 里统计**：异常请求（5xx）也要有耗时记录，否则最该看的请求恰好没有数据。
- **响应头** `X-Request-Duration-Ms` + 标准 `Server-Timing`：网关、前端、APM 都能直接采集，
  不用查库。注意这是「服务端处理耗时」，不含客户端接收时间；对 `StreamingHttpResponse`，
  头部在流开始前写入，统计的是到首字节的时间。
- **中间件放在链首**（仅次于 `SecurityMiddleware`），才能覆盖到后面所有中间件与视图的耗时。
- **落库失败不影响业务**：`RequestLog.objects.create` 包在 try/except 里，
  数据库抖动最多丢一条统计，绝不让用户请求失败。
- **可关闭、可采样**：`REQUEST_TIMING_PERSIST` / `REQUEST_TIMING_SAMPLE_RATE`。
  每请求一次 INSERT 在几千 QPS 下会成为写热点，生产建议采样或改为批量落库。
- 跳过 `/static/` 等前缀，避免静态资源刷满日志表。

---

## 7. 接口鉴权与权限模型

### 7.1 为什么是"两档"而不是 RBAC

这是个**内部运维台账**：读的人多（开发查实例、审计查变更），改的人少（DBA）。
所以只需要两档：

| 身份 | 只读 | 写 | 看明文凭据 / 轮换 / 探测 |
|---|---|---|---|
| 匿名 | 401 | 401 | 401 |
| 普通用户 | ✓ | 403 | 403 |
| staff | ✓ | ✓ | ✓ |

真要细到"某部门只能改自己的集群"，再上 `django-guardian` 做对象级权限；
现在加一层 RBAC 只会增加没人用的配置项。

### 7.2 权限判定放在请求时，不是 import 时

```python
class ApiAccessPermission(BasePermission):
    def has_permission(self, request, view):
        if not getattr(settings, "API_AUTH_REQUIRED", False):
            return True          # 本地开发：不拦
        ...                       # 生产：401 / 403
```

好处：测试可以直接 `override_settings(API_AUTH_REQUIRED=True)` 精确开关，
不需要另建一套 settings 模块，也不会出现"测试环境和生产环境权限不一致"。

### 7.3 健康检查必须放行

`/api/health/` 显式 `permission_classes = [AllowAny]`、`authentication_classes = []`。
否则 k8s 探针 / 负载均衡健康检查会拿到 401，把健康的实例判死。

### 7.4 为什么是自建 Token 而不是 JWT

- 内部系统，调用方是脚本和内部平台，不需要无状态横向扩展；
- `rest_framework.authtoken` 是 DRF 官方组件，零额外依赖，token 可随时 `--rotate` 作废；
- 接入公司 SSO 时只需换 `DEFAULT_AUTHENTICATION_CLASSES`，权限策略不动。

## 8. 未做 / 可扩展

| 项 | 说明 |
|---|---|
| 鉴权 | 现在 `AllowAny`。上线前接 SSO / Token，并对 `credential`、`rotate-password` 做二次授权 |
| 真实改密 | 轮换只改本系统记录。要真正改库需按引擎实现 `ALTER USER` 适配器（MySQL/PG/Redis 各不相同） |
| 密码下发 | 新密码需要推给使用方（Vault / K8s Secret / 工单审批），当前只返回一次明文 |
| 探测并发 | 批量探测应改为任务队列 + 并发上限，避免一次请求打满连接 |
| 统计口径 | 目前是快照。若要看趋势/环比，可再加一层「按天差分」的派生表 |
| 审计 | 密码查看/轮换目前只写 WARNING 日志，正式环境应落审计表 |
