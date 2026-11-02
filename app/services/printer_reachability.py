from __future__ import annotations

import socket
import threading
import time
from typing import Any
from urllib.parse import urlparse

from app.settings import PRINTER_IP


DEFAULT_PROBE_TIMEOUT_SECONDS = 1.5
SCHEME_DEFAULT_PORTS = {
    "http": 80,
    "https": 443,
    "ipp": 631,
    "ipps": 631,
    "lpd": 515,
    "socket": 9100,
}

# A powered-off Wi-Fi printer makes each TCP probe wait for its full timeout.
# Reuse a result for PROBE_FRESH_SECONDS. Until PROBE_STALE_SECONDS, return the
# last result at once and refresh it on one background thread, so status,
# validation, and print requests never queue behind a slow probe.
PROBE_FRESH_SECONDS = 3.0
PROBE_STALE_SECONDS = 60.0

_probe_results: dict[tuple[str, int, float], tuple[dict[str, object], float]] = {}
_probe_locks: dict[tuple[str, int, float], threading.Lock] = {}
_refreshing: set[tuple[str, int, float]] = set()
_cache_lock = threading.Lock()


def clear_probe_cache() -> None:
    with _cache_lock:
        _probe_results.clear()
        _refreshing.clear()


class InvalidPrinterEndpoint(ValueError):
    def __init__(self, message: str, *, host: str | None = None) -> None:
        super().__init__(message)
        self.host = host


def probe_printer_reachability(
    device_uri: Any,
    *,
    fallback_host: str = PRINTER_IP,
    timeout_seconds: float = DEFAULT_PROBE_TIMEOUT_SECONDS,
    max_age: float | None = None,
) -> dict[str, object]:
    """Probe the printer's port, reusing a recent result.

    max_age requires a result at most that old, probing now if needed. Print
    submission uses it so a printer switched on a moment ago is not refused.
    """
    try:
        endpoint = _endpoint_from_device_uri(device_uri, fallback_host=fallback_host)
    except InvalidPrinterEndpoint as exc:
        return {
            "checked": True,
            "host": exc.host,
            "port": None,
            "reachable": False,
            "error": str(exc),
        }

    if endpoint is None:
        return {
            "checked": False,
            "host": None,
            "port": None,
            "reachable": None,
            "error": "No printer host is configured.",
        }

    host, port = endpoint
    key = (host, port, timeout_seconds)
    fresh_for = PROBE_FRESH_SECONDS if max_age is None else min(max_age, PROBE_FRESH_SECONDS)
    with _cache_lock:
        cached = _probe_results.get(key)
        age = time.monotonic() - cached[1] if cached is not None else None
        if cached is not None and age is not None and age < fresh_for:
            return dict(cached[0])
        if max_age is None and cached is not None and age is not None and age < PROBE_STALE_SECONDS:
            if key not in _refreshing:
                _refreshing.add(key)
                threading.Thread(target=_refresh, args=(key,), daemon=True).start()
            return dict(cached[0])
        key_lock = _probe_locks.setdefault(key, threading.Lock())
    # Without a usable result, one request probes and concurrent ones wait for it.
    with key_lock:
        with _cache_lock:
            cached = _probe_results.get(key)
            if cached is not None and time.monotonic() - cached[1] < fresh_for:
                return dict(cached[0])
        return dict(_probe_and_store(key))


def _refresh(key: tuple[str, int, float]) -> None:
    try:
        _probe_and_store(key)
    finally:
        with _cache_lock:
            _refreshing.discard(key)


def _probe_and_store(key: tuple[str, int, float]) -> dict[str, object]:
    result = _probe(*key)
    with _cache_lock:
        _probe_results[key] = (result, time.monotonic())
    return result


def _probe(host: str, port: int, timeout_seconds: float) -> dict[str, object]:
    try:
        with socket.create_connection((host, port), timeout=timeout_seconds):
            return {
                "checked": True,
                "host": host,
                "port": port,
                "reachable": True,
                "error": None,
            }
    except OSError as exc:
        return {
            "checked": True,
            "host": host,
            "port": port,
            "reachable": False,
            "error": str(exc),
        }


def _endpoint_from_device_uri(
    device_uri: Any,
    *,
    fallback_host: str,
) -> tuple[str, int] | None:
    uri = str(device_uri or "")
    parsed = urlparse(uri)
    host = parsed.hostname or fallback_host
    if not host:
        return None

    try:
        port = parsed.port
    except ValueError as exc:
        raise InvalidPrinterEndpoint(
            f"Invalid printer device URI port: {exc}",
            host=host,
        ) from exc

    if port is None:
        port = SCHEME_DEFAULT_PORTS.get(parsed.scheme.lower(), 515)

    return host, port
