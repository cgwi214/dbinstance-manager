"""接口权限。

策略（`API_AUTH_REQUIRED=1` 时生效，默认关闭以便本地开箱即用）：

| 操作 | 要求 |
|---|---|
| 匿名 | 一律 401 |
| 已登录普通用户 | 只读接口（GET/HEAD/OPTIONS） |
| staff 用户 | 全部接口，含写操作与敏感动作（看明文凭据、轮换密码、端口探测） |

为什么用"读/写"而不是给每个模型配一套 RBAC：这是个内部运维台账，
读的人多、改的人少，两档足够；真要细化再上 `django-guardian` 之类。

关键点：**权限在请求时读 settings**，不是 import 时定死——
这样测试可以用 `override_settings(API_AUTH_REQUIRED=True)` 精确开关，
不需要另建一套 settings 模块。
"""
from __future__ import annotations

from django.conf import settings
from rest_framework.permissions import SAFE_METHODS, BasePermission


class ApiAccessPermission(BasePermission):
    message = "需要登录后访问（请在请求头带上 Authorization: Token <token>）"

    def has_permission(self, request, view) -> bool:
        if not getattr(settings, "API_AUTH_REQUIRED", False):
            return True

        user = getattr(request, "user", None)
        if user is None or not getattr(user, "is_authenticated", False):
            return False

        # 只读放开给所有登录用户；写操作与敏感动作要求 staff
        if request.method in SAFE_METHODS:
            return True
        return bool(getattr(user, "is_staff", False))


class IsStaff(BasePermission):
    """给"即使是 GET 也不该给普通用户看"的接口用。"""

    message = "该操作仅限管理员（staff）"

    def has_permission(self, request, view) -> bool:
        if not getattr(settings, "API_AUTH_REQUIRED", False):
            return True
        user = getattr(request, "user", None)
        return bool(user and getattr(user, "is_authenticated", False) and user.is_staff)
