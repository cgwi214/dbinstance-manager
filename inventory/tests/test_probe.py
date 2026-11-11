"""TCP 探测服务测试（用真实监听 socket，不做 mock）。"""
from __future__ import annotations

import socket
import time

from django.test import SimpleTestCase

from inventory.services import tcp_probe


def _open_listener() -> tuple[socket.socket, int]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(8)
    return sock, sock.getsockname()[1]


def _closed_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class TcpProbeServiceTests(SimpleTestCase):
    def test_reachable_when_listening(self):
        listener, port = _open_listener()
        self.addCleanup(listener.close)

        result = tcp_probe("127.0.0.1", port, timeout=2)
        self.assertTrue(result.reachable)
        self.assertEqual(result.port, port)
        self.assertIsNotNone(result.latency_ms)
        self.assertGreaterEqual(result.latency_ms, 0)
        self.assertEqual(result.error, "")
        self.assertEqual(result.resolved_ip, "127.0.0.1")

    def test_unreachable_when_port_closed(self):
        result = tcp_probe("127.0.0.1", _closed_port(), timeout=2)
        self.assertFalse(result.reachable)
        self.assertTrue(result.error)
        self.assertIsNone(result.latency_ms)

    def test_dns_failure_reported(self):
        result = tcp_probe("no-such-host.invalid", 3306, timeout=1)
        self.assertFalse(result.reachable)
        self.assertIn("DNS", result.error)

    def test_blank_host(self):
        result = tcp_probe("   ", 3306)
        self.assertFalse(result.reachable)
        self.assertEqual(result.error, "主机地址为空")

    def test_invalid_port(self):
        with self.assertRaises(ValueError):
            tcp_probe("127.0.0.1", 70000)
        with self.assertRaises(ValueError):
            tcp_probe("127.0.0.1", "abc")

    def test_timeout_is_clamped(self):
        # 超大 timeout 会被夹到上限，避免请求被长期占用
        result = tcp_probe("127.0.0.1", _closed_port(), timeout=999)
        self.assertEqual(result.timeout, 30.0)

    def test_timeout_is_a_total_budget(self):
        """timeout 是整次探测的总预算，不能被每个地址各消耗一遍。

        192.0.2.0/24 是 RFC 5737 的 TEST-NET-1，正常网络里不可达：
        要么立刻报网络不可达，要么一直等——两种情况下总耗时都必须远小于
        「地址数 × timeout」。
        """
        started = time.perf_counter()
        result = tcp_probe("192.0.2.1", 3306, timeout=1)
        elapsed = time.perf_counter() - started

        self.assertFalse(result.reachable)
        self.assertLess(elapsed, 3.0, f"耗时 {elapsed:.2f}s，timeout 预算没生效")
