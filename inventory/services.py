"""TCP 端口可达性探测。"""
from __future__ import annotations

import logging
import socket
import time
from dataclasses import asdict, dataclass

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 3.0
MIN_TIMEOUT = 0.1
MAX_TIMEOUT = 30.0


@dataclass(frozen=True)
class ProbeResult:
    host: str
    port: int
    reachable: bool
    latency_ms: float | None
    error: str = ""
    resolved_ip: str | None = None
    timeout: float = DEFAULT_TIMEOUT

    def as_dict(self) -> dict:
        return asdict(self)


def normalise_timeout(value) -> float:
    try:
        timeout = float(value)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT
    return min(max(timeout, MIN_TIMEOUT), MAX_TIMEOUT)


def normalise_port(value) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("端口必须是整数") from exc
    if not 1 <= port <= 65535:
        raise ValueError("端口必须在 1-65535 之间")
    return port


def tcp_probe(host: str, port, timeout=DEFAULT_TIMEOUT) -> ProbeResult:
    """尝试与 host:port 建立 TCP 连接。

    只做 TCP 握手（不做协议层认证），能区分
    「端口开放」/「连接被拒」/「超时」/「DNS 解析失败」四种情况。

    `timeout` 是**整次探测的总预算**，不是单个地址的预算：一个域名可能解析出多个
    地址，逐个尝试时若每个都等满 timeout，接口耗时会被放大好几倍。
    """
    host = (host or "").strip()
    port = normalise_port(port)
    timeout = normalise_timeout(timeout)

    if not host:
        return ProbeResult(host, port, False, None, "主机地址为空", None, timeout)

    try:
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        return ProbeResult(host, port, False, None, f"DNS 解析失败: {exc}", None, timeout)

    started = time.perf_counter()
    deadline = started + timeout
    last_error = "连接失败"

    for family, socktype, proto, _canonname, sockaddr in addresses:
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            last_error = f"连接超时（>{timeout}s）"
            break

        sock = socket.socket(family, socktype, proto)
        sock.settimeout(remaining)
        try:
            sock.connect(sockaddr)
        except (socket.timeout, TimeoutError):
            last_error = f"连接超时（>{timeout}s）"
            sock.close()
            continue
        except ConnectionRefusedError:
            last_error = "连接被拒绝（端口未监听）"
            sock.close()
            continue
        except OSError as exc:
            last_error = f"连接失败: {exc}"
            sock.close()
            continue

        latency_ms = round((time.perf_counter() - started) * 1000, 3)
        try:
            resolved_ip = sock.getpeername()[0]
        except OSError:  # pragma: no cover - 极少见
            resolved_ip = None
        finally:
            sock.close()
        return ProbeResult(host, port, True, latency_ms, "", resolved_ip, timeout)

    return ProbeResult(host, port, False, None, last_error, None, timeout)
