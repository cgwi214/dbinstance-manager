"""领域模型：部门 / 集群 / 实例 / 密码历史 / 每日统计 / 请求日志。"""
from __future__ import annotations

import secrets as pysecrets
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models, transaction
from django.utils import timezone

from . import crypto
from .fields import EncryptedTextField


class PasswordRotationConflict(Exception):
    """乐观锁冲突：实例密码版本已被其它进程推进。"""


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        abstract = True


class Department(TimeStampedModel):
    """部门，支持多级（parent 自关联）。"""

    name = models.CharField("部门名称", max_length=64, unique=True)
    code = models.CharField("部门编码", max_length=32, unique=True)
    parent = models.ForeignKey(
        "self",
        verbose_name="上级部门",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="children",
    )
    owner = models.CharField("负责人", max_length=64, blank=True, default="")
    description = models.TextField("描述", blank=True, default="")
    is_active = models.BooleanField("是否启用", default=True)

    class Meta:
        verbose_name = "部门"
        verbose_name_plural = "部门"
        ordering = ("code",)
        indexes = [models.Index(fields=["is_active"], name="idx_dept_active")]

    def __str__(self) -> str:
        return f"{self.name}({self.code})"

    def clean(self) -> None:
        if self.parent_id and self.pk and self.parent_id == self.pk:
            raise ValidationError({"parent": "上级部门不能是自己"})
        if self.parent_id and self.would_create_cycle(self.parent):
            raise ValidationError({"parent": "部门层级存在循环引用"})

    def would_create_cycle(self, new_parent: "Department | None") -> bool:
        """把 new_parent 挂到自己下面会不会成环。

        沿 new_parent 向上走，如果撞到自己，说明会形成 A→B→A 这样的环。
        serializer 也要调用它——`clean()` 只在 `full_clean()` 时执行，
        而 DRF 的 ModelSerializer 默认不会调 `full_clean()`。
        """
        node, guard = new_parent, 0
        while node is not None and guard < 100:
            if self.pk is not None and node.pk == self.pk:
                return True
            node = node.parent
            guard += 1
        return False

    @property
    def full_path(self) -> str:
        names, node, guard = [], self, 0
        while node is not None and guard < 20:
            names.append(node.name)
            node, guard = node.parent, guard + 1
        return " / ".join(reversed(names))


class Cluster(TimeStampedModel):
    """数据库集群，隶属于某个部门。"""

    class Engine(models.TextChoices):
        MYSQL = "mysql", "MySQL"
        POSTGRESQL = "postgresql", "PostgreSQL"
        REDIS = "redis", "Redis"
        MONGODB = "mongodb", "MongoDB"
        ORACLE = "oracle", "Oracle"
        SQLSERVER = "sqlserver", "SQL Server"

    class Environment(models.TextChoices):
        PROD = "prod", "生产"
        STAGING = "staging", "预发"
        TEST = "test", "测试"
        DEV = "dev", "开发"

    name = models.CharField("集群名称", max_length=64)
    code = models.CharField("集群编码", max_length=64, unique=True)
    department = models.ForeignKey(
        Department, verbose_name="所属部门", on_delete=models.PROTECT, related_name="clusters"
    )
    engine = models.CharField("数据库类型", max_length=16, choices=Engine.choices, default=Engine.MYSQL)
    environment = models.CharField(
        "环境", max_length=16, choices=Environment.choices, default=Environment.PROD
    )
    vip = models.CharField("集群入口地址", max_length=255, blank=True, default="")
    port = models.PositiveIntegerField(
        "集群入口端口", default=3306, validators=[MinValueValidator(1), MaxValueValidator(65535)]
    )
    description = models.TextField("描述", blank=True, default="")
    is_active = models.BooleanField("是否启用", default=True)

    class Meta:
        verbose_name = "集群"
        verbose_name_plural = "集群"
        ordering = ("department__code", "code")
        constraints = [
            models.UniqueConstraint(fields=["department", "name"], name="uniq_cluster_dept_name"),
        ]
        indexes = [
            models.Index(fields=["department", "engine"], name="idx_cluster_dept_engine"),
        ]

    def __str__(self) -> str:
        return f"{self.name}({self.code})"


class DBInstance(TimeStampedModel):
    """数据库实例。账号密码以密文落库，读取时自动解密。"""

    class Role(models.TextChoices):
        PRIMARY = "primary", "主库"
        REPLICA = "replica", "从库"
        PROXY = "proxy", "代理"

    class Status(models.TextChoices):
        ONLINE = "online", "在线"
        OFFLINE = "offline", "离线"
        MAINTENANCE = "maintenance", "维护中"
        UNKNOWN = "unknown", "未知"

    name = models.CharField("实例名称", max_length=64)
    cluster = models.ForeignKey(
        Cluster, verbose_name="所属集群", on_delete=models.PROTECT, related_name="instances"
    )
    role = models.CharField("角色", max_length=16, choices=Role.choices, default=Role.PRIMARY)
    host = models.CharField("主机地址", max_length=255)
    port = models.PositiveIntegerField(
        "端口", default=3306, validators=[MinValueValidator(1), MaxValueValidator(65535)]
    )
    engine = models.CharField(
        "数据库类型", max_length=16, choices=Cluster.Engine.choices, default=Cluster.Engine.MYSQL
    )
    status = models.CharField(
        "状态", max_length=16, choices=Status.choices, default=Status.UNKNOWN
    )

    # ---- 账号密码 ----
    account = models.CharField("账号", max_length=64, default="root")
    password_ciphertext = EncryptedTextField("密码（密文）", blank=True, default="")
    password_version = models.PositiveIntegerField("密码版本", default=0)
    password_rotated_at = models.DateTimeField("最近轮换时间", null=True, blank=True)

    # ---- 最近一次端口探测结果 ----
    last_probe_at = models.DateTimeField("最近探测时间", null=True, blank=True)
    last_probe_reachable = models.BooleanField("最近探测是否可达", null=True, blank=True)
    last_probe_latency_ms = models.FloatField("最近探测耗时(ms)", null=True, blank=True)
    last_probe_error = models.TextField("最近探测错误", blank=True, default="")

    is_active = models.BooleanField("是否启用", default=True)

    class Meta:
        verbose_name = "数据库实例"
        verbose_name_plural = "数据库实例"
        ordering = ("cluster__code", "name")
        constraints = [
            models.UniqueConstraint(fields=["cluster", "name"], name="uniq_instance_cluster_name"),
            models.UniqueConstraint(fields=["host", "port"], name="uniq_instance_host_port"),
        ]
        indexes = [
            models.Index(fields=["cluster", "status"], name="idx_instance_cluster_status"),
            models.Index(fields=["is_active"], name="idx_instance_active"),
        ]

    def __str__(self) -> str:
        return f"{self.name}@{self.host}:{self.port}"

    # ------------------------------------------------------------------ #
    # 密码
    # ------------------------------------------------------------------ #
    @property
    def password(self) -> str:
        """明文密码（内存中）；数据库中始终是密文。"""
        return self.password_ciphertext

    @password.setter
    def password(self, raw: str) -> None:
        self.set_password(raw)

    @property
    def endpoint(self) -> str:
        return f"{self.host}:{self.port}"

    def set_password(self, raw_password: str, reason: str = "manual") -> None:
        """设置密码。已存在的实例立即落库并写历史；新建实例在 save() 后补写历史。"""
        if not raw_password:
            raise ValueError("密码不能为空")
        if self._state.adding:
            # 内存中保存明文，落库时由 EncryptedTextField 统一加密
            self.password_ciphertext = raw_password
            self.password_version = (self.password_version or 0) + 1
            self.password_rotated_at = timezone.now()
            self._pending_password_history_reason = reason
            return
        self._apply_password(raw_password, reason=reason)

    def check_password(self, raw_password: str) -> bool:
        stored = self.password_ciphertext or ""
        if not stored:
            return False
        # compare_digest 对含非 ASCII 字符的 str 会抛 TypeError，统一转 bytes 比较
        return pysecrets.compare_digest(stored.encode("utf-8"), (raw_password or "").encode("utf-8"))

    def rotate_password(self, reason: str = "scheduled") -> str:
        """生成新随机密码并落库，返回明文（仅本次可见，调用方负责安全下发）。"""
        new_password = crypto.generate_password()
        self._apply_password(new_password, reason=reason)
        return new_password

    def _apply_password(self, raw_password: str, reason: str) -> None:
        now = timezone.now()
        model = type(self)
        with transaction.atomic():
            next_version = (self.password_version or 0) + 1
            # 乐观锁：只有版本号没被别人推进时才允许写入，避免并发轮换产生
            # 相同版本号的历史记录（历史表对 (instance, version) 有唯一约束）。
            # 这里传的是明文，EncryptedTextField 会在 SQL 参数阶段加密。
            updated = model.objects.filter(pk=self.pk, password_version=self.password_version).update(
                password_ciphertext=raw_password,
                password_version=next_version,
                password_rotated_at=now,
                updated_at=now,
            )
            if not updated:
                raise PasswordRotationConflict(
                    f"实例 {self.pk} 密码版本已被其它进程推进，请重新加载后再轮换"
                )
            InstancePasswordHistory.objects.create(
                instance_id=self.pk,
                account=self.account,
                password_ciphertext=raw_password,
                version=next_version,
                reason=reason,
            )
        self.password_ciphertext = raw_password
        self.password_version = next_version
        self.password_rotated_at = now

    @property
    def password_is_stale(self) -> bool:
        hours = getattr(settings, "INSTANCE_PASSWORD_ROTATION_HOURS", 12)
        if self.password_rotated_at is None:
            return True
        return timezone.now() - self.password_rotated_at > timedelta(hours=hours)

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        reason = getattr(self, "_pending_password_history_reason", None)
        if reason:
            self._pending_password_history_reason = None
            InstancePasswordHistory.objects.create(
                instance=self,
                account=self.account,
                password_ciphertext=self.password_ciphertext,
                version=self.password_version,
                reason=reason,
            )

    def record_probe(self, result) -> None:
        """把一次 TCP 探测结果写回实例。

        `maintenance` 是人工设置的状态，探测不去覆盖它——否则运维刚把实例
        标成维护中，一次成功的探测就把它改回 online 了。
        """
        self.last_probe_at = timezone.now()
        self.last_probe_reachable = result.reachable
        self.last_probe_latency_ms = result.latency_ms
        self.last_probe_error = result.error or ""
        if self.status == self.Status.MAINTENANCE:
            pass
        elif result.reachable:
            self.status = self.Status.ONLINE
        elif self.status == self.Status.ONLINE:
            # 只把"原本在线"的改判离线，避免把维护中的实例误标为离线
            self.status = self.Status.OFFLINE
        self.save(
            update_fields=[
                "last_probe_at",
                "last_probe_reachable",
                "last_probe_latency_ms",
                "last_probe_error",
                "status",
                "updated_at",
            ]
        )


class InstancePasswordHistory(models.Model):
    """密码轮换历史（仅追加），用于审计与应急回溯。"""

    class Reason(models.TextChoices):
        SCHEDULED = "scheduled", "定时轮换"
        MANUAL = "manual", "手工修改"
        RESET = "reset", "重置"

    instance = models.ForeignKey(
        DBInstance, verbose_name="实例", on_delete=models.CASCADE, related_name="password_history"
    )
    account = models.CharField("账号", max_length=64)
    password_ciphertext = EncryptedTextField("历史密码（密文）", blank=True, default="")
    version = models.PositiveIntegerField("密码版本")
    reason = models.CharField("变更原因", max_length=16, choices=Reason.choices, default=Reason.SCHEDULED)
    rotated_at = models.DateTimeField("轮换时间", auto_now_add=True)

    class Meta:
        verbose_name = "密码历史"
        verbose_name_plural = "密码历史"
        ordering = ("-rotated_at", "-id")
        constraints = [
            models.UniqueConstraint(fields=["instance", "version"], name="uniq_pwdhist_instance_version"),
        ]

    def __str__(self) -> str:
        return f"{self.instance_id}#v{self.version}"


class InstanceDailyStat(models.Model):
    """按 (日期, 部门, 集群) 维度的实例数量快照。"""

    stat_date = models.DateField("统计日期")
    department = models.ForeignKey(
        Department, verbose_name="部门", on_delete=models.PROTECT, related_name="daily_stats"
    )
    cluster = models.ForeignKey(
        Cluster, verbose_name="集群", on_delete=models.PROTECT, related_name="daily_stats"
    )
    total_instances = models.PositiveIntegerField("实例总数", default=0)
    active_instances = models.PositiveIntegerField("启用实例数", default=0)
    online_instances = models.PositiveIntegerField("在线实例数", default=0)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        verbose_name = "每日实例统计"
        verbose_name_plural = "每日实例统计"
        ordering = ("-stat_date", "department__code", "cluster__code")
        constraints = [
            models.UniqueConstraint(
                fields=["stat_date", "department", "cluster"], name="uniq_stat_date_dept_cluster"
            ),
        ]
        indexes = [models.Index(fields=["stat_date"], name="idx_stat_date")]

    def __str__(self) -> str:
        return f"{self.stat_date} {self.department_id}/{self.cluster_id}={self.total_instances}"


class RequestLog(models.Model):
    """请求耗时明细（由中间件写入）。"""

    method = models.CharField(max_length=10)
    path = models.CharField(max_length=512)
    status_code = models.PositiveIntegerField(default=0)
    duration_ms = models.FloatField(default=0.0)
    user_repr = models.CharField(max_length=150, blank=True, default="")
    client_ip = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "请求日志"
        verbose_name_plural = "请求日志"
        ordering = ("-id",)
        indexes = [models.Index(fields=["-created_at", "duration_ms"], name="idx_reqlog_time_dur")]

    def __str__(self) -> str:
        return f"{self.method} {self.path} {self.status_code} {self.duration_ms:.1f}ms"
