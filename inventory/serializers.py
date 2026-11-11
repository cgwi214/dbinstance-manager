"""序列化器：密码只进不出（除显式轮换/查看接口）。"""
from __future__ import annotations

from rest_framework import serializers

from . import crypto
from .models import (
    Cluster,
    DBInstance,
    Department,
    InstanceDailyStat,
    InstancePasswordHistory,
    RequestLog,
)


class DepartmentSerializer(serializers.ModelSerializer):
    parent_name = serializers.CharField(source="parent.name", read_only=True, default=None)
    full_path = serializers.CharField(read_only=True)
    cluster_count = serializers.IntegerField(read_only=True, required=False)
    instance_count = serializers.IntegerField(read_only=True, required=False)

    class Meta:
        model = Department
        fields = [
            "id",
            "name",
            "code",
            "parent",
            "parent_name",
            "full_path",
            "owner",
            "description",
            "is_active",
            "cluster_count",
            "instance_count",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate_code(self, value: str) -> str:
        return value.strip().upper()

    def validate(self, attrs):
        parent = attrs.get("parent", getattr(self.instance, "parent", None))
        if self.instance is not None and parent is not None:
            if parent.pk == self.instance.pk:
                raise serializers.ValidationError({"parent": "上级部门不能是自己"})
            # DRF 不会调 model.full_clean()，环检测必须在这里显式做一次
            if self.instance.would_create_cycle(parent):
                raise serializers.ValidationError({"parent": "部门层级存在循环引用"})
        return attrs


class ClusterSerializer(serializers.ModelSerializer):
    department_name = serializers.CharField(source="department.name", read_only=True)
    department_code = serializers.CharField(source="department.code", read_only=True)
    instance_count = serializers.IntegerField(read_only=True, required=False)
    engine_display = serializers.CharField(source="get_engine_display", read_only=True)
    environment_display = serializers.CharField(source="get_environment_display", read_only=True)

    class Meta:
        model = Cluster
        fields = [
            "id",
            "name",
            "code",
            "department",
            "department_name",
            "department_code",
            "engine",
            "engine_display",
            "environment",
            "environment_display",
            "vip",
            "port",
            "description",
            "is_active",
            "instance_count",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


class DBInstanceSerializer(serializers.ModelSerializer):
    cluster_name = serializers.CharField(source="cluster.name", read_only=True)
    department = serializers.IntegerField(source="cluster.department_id", read_only=True)
    department_name = serializers.CharField(source="cluster.department.name", read_only=True)
    password = serializers.CharField(
        write_only=True,
        required=False,
        allow_blank=False,
        trim_whitespace=False,
        min_length=8,
        max_length=128,
        help_text="明文密码，仅用于写入；不会出现在任何响应中",
    )
    endpoint = serializers.CharField(read_only=True)
    password_is_stale = serializers.BooleanField(read_only=True)
    role_display = serializers.CharField(source="get_role_display", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = DBInstance
        fields = [
            "id",
            "name",
            "cluster",
            "cluster_name",
            "department",
            "department_name",
            "role",
            "role_display",
            "host",
            "port",
            "endpoint",
            "engine",
            "status",
            "status_display",
            "account",
            "password",
            "password_version",
            "password_rotated_at",
            "password_is_stale",
            "last_probe_at",
            "last_probe_reachable",
            "last_probe_latency_ms",
            "last_probe_error",
            "is_active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "password_version",
            "password_rotated_at",
            "last_probe_at",
            "last_probe_reachable",
            "last_probe_latency_ms",
            "last_probe_error",
            "created_at",
            "updated_at",
        ]

    def validate_host(self, value: str) -> str:
        value = (value or "").strip()
        if not value:
            raise serializers.ValidationError("主机地址不能为空")
        return value

    def create(self, validated_data):
        raw_password = validated_data.pop("password", None)
        generated = None
        if raw_password is None:
            # 未提供密码时自动生成一个，并在响应里回显一次
            generated = crypto.generate_password()
            raw_password = generated
        instance = DBInstance(**validated_data)
        instance.set_password(
            raw_password,
            reason=(
                InstancePasswordHistory.Reason.MANUAL
                if generated is None
                else InstancePasswordHistory.Reason.RESET
            ),
        )
        instance.save()
        if generated:
            instance.generated_password = generated
        return instance

    def update(self, instance, validated_data):
        raw_password = validated_data.pop("password", None)
        for field, value in validated_data.items():
            setattr(instance, field, value)
        if raw_password:
            instance.set_password(raw_password, reason=InstancePasswordHistory.Reason.MANUAL)
            if validated_data:
                instance.save(update_fields=[*validated_data.keys(), "updated_at"])
        else:
            instance.save()
        return instance

    def to_representation(self, instance):
        data = super().to_representation(instance)
        generated = getattr(instance, "generated_password", None)
        if generated:
            data["generated_password"] = generated
        return data


class InstancePasswordHistorySerializer(serializers.ModelSerializer):
    """只暴露元信息，不回显历史密码明文。"""

    class Meta:
        model = InstancePasswordHistory
        fields = ["id", "instance", "account", "version", "reason", "rotated_at"]
        read_only_fields = fields


class CredentialRequestSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=200, help_text="查看明文凭据的原因，会写入日志")

    def validate_reason(self, value: str) -> str:
        value = (value or "").strip()
        if len(value) < 4:
            raise serializers.ValidationError("请填写不少于 4 个字的查看原因（用于审计）")
        return value


class InstanceDailyStatSerializer(serializers.ModelSerializer):
    department_name = serializers.CharField(source="department.name", read_only=True)
    department_code = serializers.CharField(source="department.code", read_only=True)
    cluster_name = serializers.CharField(source="cluster.name", read_only=True)
    cluster_code = serializers.CharField(source="cluster.code", read_only=True)

    class Meta:
        model = InstanceDailyStat
        fields = [
            "id",
            "stat_date",
            "department",
            "department_name",
            "department_code",
            "cluster",
            "cluster_name",
            "cluster_code",
            "total_instances",
            "active_instances",
            "online_instances",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


class RequestLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = RequestLog
        fields = ["id", "method", "path", "status_code", "duration_ms", "user_repr", "client_ip", "created_at"]
        read_only_fields = fields
