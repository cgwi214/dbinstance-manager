"""请求耗时中间件测试。"""
from __future__ import annotations

from django.test import Client, TestCase, override_settings

from inventory.models import RequestLog

BASE_CONFIG = {
    "header": "X-Request-Duration-Ms",
    "persist": True,
    "sample_rate": 1.0,
    "slow_threshold_ms": 500.0,
    "skip_prefixes": ("/static/", "/media/", "/favicon.ico", "/admin/jsi18n/"),
}


class RequestTimingMiddlewareTests(TestCase):
    def test_duration_header_and_log_row(self):
        response = self.client.get("/api/health/")

        self.assertEqual(response.status_code, 200)
        self.assertIn("X-Request-Duration-Ms", response)
        self.assertIn("Server-Timing", response)

        duration = float(response["X-Request-Duration-Ms"])
        self.assertGreater(duration, 0)
        self.assertLess(duration, 60_000)

        log = RequestLog.objects.get(path="/api/health/")
        self.assertEqual(log.method, "GET")
        self.assertEqual(log.status_code, 200)
        self.assertAlmostEqual(log.duration_ms, duration, delta=max(1.0, duration * 0.5))

    def test_not_found_request_is_still_recorded(self):
        response = self.client.get("/api/instances/999999/")
        self.assertEqual(response.status_code, 404)
        self.assertTrue(
            RequestLog.objects.filter(path="/api/instances/999999/", status_code=404).exists()
        )

    def test_static_prefix_skipped(self):
        self.client.get("/static/app.css")
        self.assertFalse(RequestLog.objects.filter(path__startswith="/static/").exists())

    def test_persist_can_be_disabled(self):
        config = dict(BASE_CONFIG, persist=False)
        with override_settings(REQUEST_TIMING=config):
            response = Client().get("/api/health/")

        self.assertIn("X-Request-Duration-Ms", response)
        self.assertFalse(RequestLog.objects.exists())

    def test_custom_header_name(self):
        config = dict(BASE_CONFIG, header="X-Cost-Ms", persist=False)
        with override_settings(REQUEST_TIMING=config):
            response = Client().get("/api/health/")
        self.assertIn("X-Cost-Ms", response)

    def test_records_path_with_query_string(self):
        self.client.get("/api/instances/?department=1")
        self.assertTrue(
            RequestLog.objects.filter(path__startswith="/api/instances/?department=1").exists()
        )

    def test_client_ip_from_forwarded_header(self):
        self.client.get("/api/health/", HTTP_X_FORWARDED_FOR="203.0.113.9, 10.0.0.1")
        log = RequestLog.objects.filter(path="/api/health/").first()
        self.assertEqual(log.client_ip, "203.0.113.9")
