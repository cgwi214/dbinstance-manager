"""统一分页。

DRF 的 `PageNumberPagination` 默认**不认** `?page_size=`，调用方传了也被静默忽略——
排查起来很费劲（看起来"参数没生效"）。这里显式支持，并给一个上限防止被拉爆。
"""
from __future__ import annotations

from rest_framework.pagination import PageNumberPagination


class StandardPagination(PageNumberPagination):
    page_size_query_param = "page_size"
    max_page_size = 200
