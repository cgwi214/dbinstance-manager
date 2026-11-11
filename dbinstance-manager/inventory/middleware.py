"""请求耗时中间件。

职责：
1. 测量每个请求的处理耗时（perf_counter，单调时钟，不受系统时间调整影响）；
2. 通过响应头 `X-Request-Duration-Ms` / `Server-Timing` 暴露耗时，便于网关与前端采集；
3. 把耗时写入 `RequestLog`（可关闭、可采样），慢请求额外打 WARNING 日志。

注意：耗时统计在 `finally` 中执行，异常请求同样会被记录，不会漏掉 5xx。
"""
from __future__ import annotations

import logging
import random
import time

from django.conf import settings

logger = logging.getLogger("inventory.middleware")


class RequestTimingMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response
        config = getattr(settings, "REQUEST_TIMING", None) or {}
        self.header = config.get("header", "X-Request-Duration-Ms")
        self.persist = config.get("persist", True)
        self.sample_rate = float(config.get("sample_rate", 1.0))
        self.slow_threshold_ms = float(config.get("slow_threshold_ms", 500.0))
        self.skip_prefixes = tuple(config.get("skip_prefixes", ()))
        self._rng = random.Random()

    def __call__(self, request):
        if self._should_skip(request):
            return self.get_response(request)

        started = time.perf_counter()
        response = None
        try:
            response = self.get_response(request)
            return response
        finally:
            duration_ms = (time.perf_counter() - started) * 1000.0
            request.duration_ms = duration_ms  # 供视图/日志复用
            if response is not None:
                # 响应头必须在响应体流出前设置，因此这里统计的是“服务端处理耗时”
                # （不含客户端接收时间）
                response[self.header] = f"{duration_ms:.2f}"
                response["Server-Timing"] = f"app;dur={duration_ms:.2f}"
            try:
                self._handle(request, response, duration_ms)
            except Exception:  # pragma: no cover
                # 计时/落库是旁路逻辑，绝不能在 finally 里盖掉业务异常
                logger.exception("请求耗时统计失败 path=%s", getattr(request, "path", "?"))

    def _should_skip(self, request) -> bool:
        path = request.path or ""
        return any(path.startswith(prefix) for prefix in self.skip_prefixes)

    def _handle(self, request, response, duration_ms: float) -> None:
        status_code = getattr(response, "status_code", 500 if response is None else 0)
        if duration_ms >= self.slow_threshold_ms:
            logger.warning(
                "慢请求 %s %s -> %s 耗时 %.2fms",
                request.method,
                request.get_full_path(),
                status_code,
                duration_ms,
            )
        else:
            logger.debug(
                "%s %s -> %s 耗时 %.2fms", request.method, request.get_full_path(), status_code, duration_ms
            )

        if not self.persist:
            return
        if self.sample_rate < 1.0 and self._rng.random() > self.sample_rate:
            return

        # 延迟导入，避免 app 尚未加载完成时（如 migrate 早期）导入模型
        from .models import RequestLog

        try:
            RequestLog.objects.create(
                method=(request.method or "")[:10],
                path=(request.get_full_path() or "")[:512],
                status_code=status_code,
                duration_ms=round(duration_ms, 3),
                user_repr=self._user_repr(request)[:150],
                client_ip=self._client_ip(request)[:64],
            )
        except Exception:  # pragma: no cover - 统计失败绝不能影响业务请求
            logger.exception("写入请求耗时日志失败 path=%s", getattr(request, "path", "?"))

    @staticmethod
    def _user_repr(request) -> str:
        user = getattr(request, "user", None)
        if user is None:
            return ""
        try:
            if getattr(user, "is_authenticated", False):
                return str(user)
        except Exception:  # pragma: no cover
            return ""
        return ""

    @staticmethod
    def _client_ip(request) -> str:
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return request.META.get("REMOTE_ADDR", "") or ""
