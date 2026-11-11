"""REST API 视图。"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from django.conf import settings
from django.db import connections
from django.db.models import Count, F, ProtectedError, Q, Sum
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import APIException
from rest_framework.response import Response
from rest_framework.permissions import AllowAny
from rest_framework.views import APIView

from .models import (
    Cluster,
    DBInstance,
    Department,
    InstanceDailyStat,
    InstancePasswordHistory,
    RequestLog,
)
from .serializers import (
    ClusterSerializer,
    CredentialRequestSerializer,
    DBInstanceSerializer,
    DepartmentSerializer,
    InstanceDailyStatSerializer,
    InstancePasswordHistorySerializer,
    RequestLogSerializer,
)
from .services import DEFAULT_TIMEOUT, tcp_probe
from .tasks import collect_daily_instance_stats, run_sync

logger = logging.getLogger("inventory.views")

TRUE_VALUES = {"1", "true", "yes", "on"}


class Conflict(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = "资源仍被其它数据引用，无法删除"
    default_code = "conflict"


class ProtectedDestroyMixin:
    """把 PROTECT 外键抛出的 ProtectedError 转成 409，而不是 500。"""

    def perform_destroy(self, instance):
        try:
            instance.delete()
        except ProtectedError as exc:
            related = sorted({obj.__class__.__name__ for obj in exc.protected_objects})
            raise Conflict(f"该记录仍被 {', '.join(related)} 引用，请先处理关联数据（或改为停用）") from exc


def _as_bool(value) -> bool:
    return str(value).strip().lower() in TRUE_VALUES


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_date(value):
    if not value:
        return None
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except ValueError:
        return None


def _iso(value):
    """统一把时间序列化成 settings.TIME_ZONE 下的 ISO 字符串。

    DRF 的 DateTimeField 会做 localtime 转换，手工 isoformat 不会；
    两处混用就会出现同一个响应里既有 +08:00 又有 +00:00。
    """
    if value is None:
        return None
    if timezone.is_aware(value):
        value = timezone.localtime(value)
    return value.isoformat()


# --------------------------------------------------------------------------- #
# 部门
# --------------------------------------------------------------------------- #
class DepartmentViewSet(ProtectedDestroyMixin, viewsets.ModelViewSet):
    """部门增删改查。GET /api/departments/?search=&is_active=&parent="""

    serializer_class = DepartmentSerializer
    queryset = Department.objects.all()

    def get_queryset(self):
        queryset = Department.objects.select_related("parent").annotate(
            cluster_count=Count("clusters", distinct=True),
            instance_count=Count("clusters__instances", distinct=True),
        )
        params = self.request.query_params

        if (value := params.get("is_active")) is not None:
            queryset = queryset.filter(is_active=_as_bool(value))
        if (value := params.get("search")):
            queryset = queryset.filter(Q(name__icontains=value) | Q(code__icontains=value))
        if (value := params.get("parent")) is not None:
            queryset = (
                queryset.filter(parent__isnull=True)
                if value.lower() in {"null", "none", "root"}
                else queryset.filter(parent_id=_as_int(value) or 0)
            )
        if params.get("ordering") in {"name", "-name", "code", "-code", "created_at", "-created_at"}:
            return queryset.order_by(params["ordering"])
        return queryset.order_by("code")


# --------------------------------------------------------------------------- #
# 集群
# --------------------------------------------------------------------------- #
class ClusterViewSet(ProtectedDestroyMixin, viewsets.ModelViewSet):
    """集群增删改查。GET /api/clusters/?department=&engine=&environment=&search="""

    serializer_class = ClusterSerializer
    queryset = Cluster.objects.all()

    def get_queryset(self):
        queryset = Cluster.objects.select_related("department").annotate(
            instance_count=Count("instances", distinct=True)
        )
        params = self.request.query_params

        for field in ("department", "engine", "environment"):
            if (value := params.get(field)) is not None:
                key = "department_id" if field == "department" else field
                queryset = queryset.filter(**{key: _as_int(value) if field == "department" else value})
        if (value := params.get("is_active")) is not None:
            queryset = queryset.filter(is_active=_as_bool(value))
        if (value := params.get("search")):
            queryset = queryset.filter(
                Q(name__icontains=value) | Q(code__icontains=value) | Q(vip__icontains=value)
            )
        if params.get("ordering") in {"name", "-name", "code", "-code", "created_at", "-created_at"}:
            return queryset.order_by(params["ordering"])
        return queryset.order_by("department__code", "code")


# --------------------------------------------------------------------------- #
# 实例
# --------------------------------------------------------------------------- #
class DBInstanceViewSet(ProtectedDestroyMixin, viewsets.ModelViewSet):
    """实例增删改查 + 端口探测 + 密码轮换/查看/历史。

    列表参数：cluster / department / engine / status / role / is_active / search / stale
    """

    serializer_class = DBInstanceSerializer
    queryset = DBInstance.objects.all()

    def get_queryset(self):
        queryset = DBInstance.objects.select_related("cluster", "cluster__department")
        params = self.request.query_params

        if (value := _as_int(params.get("cluster"))):
            queryset = queryset.filter(cluster_id=value)
        if (value := _as_int(params.get("department"))):
            queryset = queryset.filter(cluster__department_id=value)
        for field in ("engine", "status", "role"):
            if (value := params.get(field)) is not None:
                queryset = queryset.filter(**{field: value})
        if (value := params.get("is_active")) is not None:
            queryset = queryset.filter(is_active=_as_bool(value))
        if (value := params.get("search")):
            queryset = queryset.filter(
                Q(name__icontains=value) | Q(host__icontains=value) | Q(account__icontains=value)
            )
        if (value := params.get("stale")) is not None and _as_bool(value):
            hours = self._rotation_hours()
            threshold = timezone.now() - timedelta(hours=hours)
            queryset = queryset.filter(
                Q(password_rotated_at__isnull=True) | Q(password_rotated_at__lt=threshold)
            )
        if params.get("ordering") in {"name", "-name", "created_at", "-created_at", "host", "-host"}:
            return queryset.order_by(params["ordering"])
        return queryset.order_by("cluster__code", "name")

    @staticmethod
    def _rotation_hours() -> int:
        return int(getattr(settings, "INSTANCE_PASSWORD_ROTATION_HOURS", 12))

    # ---------------- 需求 2：TCP 端口可达性探测 ---------------- #
    @action(detail=True, methods=["post"], url_path="probe")
    def probe(self, request, pk=None):
        instance = self.get_object()
        timeout = request.data.get("timeout", DEFAULT_TIMEOUT)
        result = tcp_probe(instance.host, instance.port, timeout)
        instance.record_probe(result)
        payload = {
            "instance_id": instance.pk,
            "name": instance.name,
            "endpoint": instance.endpoint,
            **result.as_dict(),
            "probed_at": _iso(timezone.now()),
        }
        return Response(payload, status=status.HTTP_200_OK)

    # ---------------- 需求 3：密码管理 ---------------- #
    @action(detail=True, methods=["post"], url_path="rotate-password")
    def rotate_password(self, request, pk=None):
        """立即轮换该实例密码，明文只在本次响应中出现一次。"""
        instance = self.get_object()
        new_password = instance.rotate_password(reason=InstancePasswordHistory.Reason.MANUAL)
        logger.warning(
            "手动轮换密码 instance_id=%s operator=%s", instance.pk, self._operator(request)
        )
        return Response(
            {
                "instance_id": instance.pk,
                "account": instance.account,
                "password": new_password,
                "password_version": instance.password_version,
                "rotated_at": _iso(instance.password_rotated_at),
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"], url_path="credential")
    def credential(self, request, pk=None):
        """查看当前凭据明文。必须填写原因，便于审计。"""
        instance = self.get_object()
        serializer = CredentialRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        logger.warning(
            "查看实例凭据 instance_id=%s operator=%s reason=%s",
            instance.pk,
            self._operator(request),
            serializer.validated_data["reason"],
        )
        return Response(
            {
                "instance_id": instance.pk,
                "endpoint": instance.endpoint,
                "account": instance.account,
                "password": instance.password,
                "password_version": instance.password_version,
                "password_rotated_at": _iso(instance.password_rotated_at),
            }
        )

    @action(detail=True, methods=["get"], url_path="password-history")
    def password_history(self, request, pk=None):
        instance = self.get_object()
        history = instance.password_history.all()[:100]
        return Response(InstancePasswordHistorySerializer(history, many=True).data)

    @staticmethod
    def _operator(request) -> str:
        user = getattr(request, "user", None)
        if user is not None and getattr(user, "is_authenticated", False):
            return str(user)
        return "anonymous"


# --------------------------------------------------------------------------- #
# 需求 4：统计查询
# --------------------------------------------------------------------------- #
class InstanceDailyStatViewSet(viewsets.ReadOnlyModelViewSet):
    """GET /api/stats/daily/?date=2026-11-10&department=&cluster=&start=&end="""

    serializer_class = InstanceDailyStatSerializer
    queryset = InstanceDailyStat.objects.all()

    def get_queryset(self):
        queryset = InstanceDailyStat.objects.select_related("department", "cluster")
        params = self.request.query_params

        if (day := _parse_date(params.get("date"))):
            queryset = queryset.filter(stat_date=day)
        if (start := _parse_date(params.get("start"))):
            queryset = queryset.filter(stat_date__gte=start)
        if (end := _parse_date(params.get("end"))):
            queryset = queryset.filter(stat_date__lte=end)
        if (value := _as_int(params.get("department"))):
            queryset = queryset.filter(department_id=value)
        if (value := _as_int(params.get("cluster"))):
            queryset = queryset.filter(cluster_id=value)
        return queryset.order_by("-stat_date", "department__code", "cluster__code")

    @action(detail=False, methods=["get"], url_path="summary")
    def summary(self, request):
        """按部门汇总某一天的统计（数据来源仍是已落库的统计表）。"""
        queryset = self.get_queryset()
        day = _parse_date(request.query_params.get("date"))
        if day is None:
            latest = InstanceDailyStat.objects.order_by("-stat_date").values_list("stat_date", flat=True).first()
            if latest is None:
                return Response({"stat_date": None, "departments": [], "total_instances": 0})
            day = latest
            queryset = queryset.filter(stat_date=day)

        rows = (
            queryset.filter(stat_date=day)
            .values(
                "department_id",
                department_name=F("department__name"),
                department_code=F("department__code"),
            )
            .annotate(
                clusters=Count("cluster", distinct=True),
                total_instances=Sum("total_instances"),
                active_instances=Sum("active_instances"),
                online_instances=Sum("online_instances"),
            )
            .order_by("department_code")
        )
        departments = list(rows)
        return Response(
            {
                "stat_date": day.isoformat(),
                "departments": departments,
                "total_instances": sum(row["total_instances"] or 0 for row in departments),
            }
        )

    @action(detail=False, methods=["post"], url_path="collect")
    def collect(self, request):
        """手动触发统计任务（默认统计今天）。

        生产（有 broker）：投递到 Celery，返回 202 + task_id。
        本地（CELERY_TASK_ALWAYS_EAGER=1）：直接同步执行并返回结果，
        这样没有 Redis 也能完整走通这个接口。
        """
        day = _parse_date(request.data.get("stat_date")) or timezone.localdate()

        if getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False):
            summary = run_sync(collect_daily_instance_stats, stat_date=day.isoformat())
            return Response(
                {"state": "SUCCESS", "stat_date": day.isoformat(), "result": summary},
                status=status.HTTP_200_OK,
            )

        async_result = collect_daily_instance_stats.delay(day.isoformat())
        payload = {
            "task_id": getattr(async_result, "id", None),
            "state": getattr(async_result, "state", "PENDING"),
            "stat_date": day.isoformat(),
        }
        try:
            if async_result.ready():
                payload["result"] = async_result.result
        except Exception:  # noqa: BLE001 - 结果后端不可用时不影响投递结果
            logger.warning("读取任务结果失败 task_id=%s", payload["task_id"], exc_info=True)
        return Response(payload, status=status.HTTP_202_ACCEPTED)


# --------------------------------------------------------------------------- #
# 需求 5：请求耗时查询（中间件落库数据的出口）
# --------------------------------------------------------------------------- #
class RequestLogViewSet(viewsets.ReadOnlyModelViewSet):
    """GET /api/request-logs/?path=&min_duration=&since_id="""

    serializer_class = RequestLogSerializer
    queryset = RequestLog.objects.all()

    def get_queryset(self):
        queryset = RequestLog.objects.all()
        params = self.request.query_params
        if (value := params.get("path")):
            queryset = queryset.filter(path__icontains=value)
        if (value := params.get("method")):
            queryset = queryset.filter(method=value.upper())
        if (value := params.get("min_duration")):
            try:
                queryset = queryset.filter(duration_ms__gte=float(value))
            except ValueError:
                pass
        if (value := _as_int(params.get("since_id"))):
            queryset = queryset.filter(id__gt=value)
        return queryset.order_by("-id")

    @action(detail=False, methods=["get"], url_path="stats")
    def stats(self, request):
        """最近 N 条请求的耗时分布（默认 1000 条）。"""
        limit = min(max(_as_int(request.query_params.get("limit")) or 1000, 1), 10000)
        durations = sorted(
            RequestLog.objects.order_by("-id").values_list("duration_ms", flat=True)[:limit]
        )
        if not durations:
            return Response({"count": 0, "avg_ms": None, "p50_ms": None, "p95_ms": None, "max_ms": None})

        def percentile(ratio: float) -> float:
            index = min(int(len(durations) * ratio), len(durations) - 1)
            return round(durations[index], 3)

        return Response(
            {
                "count": len(durations),
                "avg_ms": round(sum(durations) / len(durations), 3),
                "p50_ms": percentile(0.50),
                "p95_ms": percentile(0.95),
                "max_ms": round(durations[-1], 3),
            }
        )


# --------------------------------------------------------------------------- #
# 健康检查
# --------------------------------------------------------------------------- #
class HealthView(APIView):
    # 健康检查要给 k8s / 负载均衡探针用，不能要求 token
    authentication_classes: list = []
    permission_classes = [AllowAny]

    def get(self, request):
        db_ok = True
        db_error = ""
        try:
            with connections["default"].cursor() as cursor:
                cursor.execute("SELECT 1")
        except Exception as exc:  # noqa: BLE001
            db_ok = False
            db_error = str(exc)[:200]

        try:
            key_ok = bool(getattr(settings, "INSTANCE_ENCRYPTION_KEYS", []))
        except Exception:  # noqa: BLE001
            key_ok = False

        return Response(
            {
                "status": "ok" if db_ok else "degraded",
                "database": db_ok,
                "database_error": db_error,
                "encryption_key_configured": key_ok,
                "time": _iso(timezone.now()),
            },
            status=status.HTTP_200_OK if db_ok else status.HTTP_503_SERVICE_UNAVAILABLE,
        )
