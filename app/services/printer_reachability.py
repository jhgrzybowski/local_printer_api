from __future__ import annotations

import socket
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


class InvalidPrinterEndpoint(ValueError):
    def __init__(self, message: str, *, host: str | None = None) -> None:
        super().__init__(message)
        self.host = host


def probe_printer_reachability(
    device_uri: Any,
    *,
    fallback_host: str = PRINTER_IP,
    timeout_seconds: float = DEFAULT_PROBE_TIMEOUT_SECONDS,
) -> dict[str, object]:
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
