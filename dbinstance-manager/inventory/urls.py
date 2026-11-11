"""inventory 路由。"""
from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    ClusterViewSet,
    DBInstanceViewSet,
    DepartmentViewSet,
    HealthView,
    InstanceDailyStatViewSet,
    RequestLogViewSet,
)

router = DefaultRouter()
router.register("departments", DepartmentViewSet, basename="department")
router.register("clusters", ClusterViewSet, basename="cluster")
router.register("instances", DBInstanceViewSet, basename="instance")
router.register("stats/daily", InstanceDailyStatViewSet, basename="daily-stat")
router.register("request-logs", RequestLogViewSet, basename="request-log")

urlpatterns = [
    path("", include(router.urls)),
    path("health/", HealthView.as_view(), name="health"),
]
