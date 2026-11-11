"""Django Admin（运维应急入口）。"""
from django.contrib import admin

from .models import (
    Cluster,
    DBInstance,
    Department,
    InstanceDailyStat,
    InstancePasswordHistory,
    RequestLog,
)


class ClusterInline(admin.TabularInline):
    model = Cluster
    extra = 0
    fields = ("name", "code", "engine", "environment", "is_active")


@admin.register(Department)
class DepartmentAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "parent", "owner", "is_active", "created_at")
    list_filter = ("is_active",)
    search_fields = ("code", "name", "owner")
    inlines = [ClusterInline]


@admin.register(Cluster)
class ClusterAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "department", "engine", "environment", "vip", "port", "is_active")
    list_filter = ("engine", "environment", "is_active", "department")
    search_fields = ("code", "name", "vip")


@admin.register(DBInstance)
class DBInstanceAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "cluster",
        "role",
        "host",
        "port",
        "status",
        "account",
        "password_version",
        "password_rotated_at",
        "is_active",
    )
    list_filter = ("status", "role", "engine", "is_active", "cluster__department")
    search_fields = ("name", "host", "account")
    readonly_fields = (
        "password_version",
        "password_rotated_at",
        "last_probe_at",
        "last_probe_reachable",
        "last_probe_latency_ms",
        "last_probe_error",
        "created_at",
        "updated_at",
    )
    # 密码密文字段不在 Admin 中直接编辑，避免误改
    exclude = ("password_ciphertext",)


@admin.register(InstancePasswordHistory)
class InstancePasswordHistoryAdmin(admin.ModelAdmin):
    list_display = ("instance", "account", "version", "reason", "rotated_at")
    list_filter = ("reason",)
    exclude = ("password_ciphertext",)


@admin.register(InstanceDailyStat)
class InstanceDailyStatAdmin(admin.ModelAdmin):
    list_display = (
        "stat_date",
        "department",
        "cluster",
        "total_instances",
        "active_instances",
        "online_instances",
    )
    list_filter = ("stat_date", "department")
    date_hierarchy = "stat_date"


@admin.register(RequestLog)
class RequestLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "method", "path", "status_code", "duration_ms", "client_ip")
    list_filter = ("method", "status_code")
    search_fields = ("path",)
    date_hierarchy = "created_at"
