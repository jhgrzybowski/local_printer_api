from __future__ import annotations

import socket

from app.services import printer_reachability
from app.services.printer_reachability import probe_printer_reachability


class DummyConnection:
    def __enter__(self) -> "DummyConnection":
        return self

    def __exit__(self, *_args: object) -> None:
        return None


def test_probe_uses_lpd_port_from_device_uri(monkeypatch) -> None:
    calls: list[tuple[tuple[str, int], float | None]] = []

    def fake_create_connection(
        address: tuple[str, int],
        timeout: float | None = None,
    ) -> DummyConnection:
        calls.append((address, timeout))
        return DummyConnection()

    monkeypatch.setattr(socket, "create_connection", fake_create_connection)

    result = probe_printer_reachability("lpd://192.168.100.100/PASSTHRU")

    assert calls == [
        (("192.168.100.100", 515), printer_reachability.DEFAULT_PROBE_TIMEOUT_SECONDS)
    ]
    assert result == {
        "checked": True,
        "host": "192.168.100.100",
        "port": 515,
        "reachable": True,
        "error": None,
    }


def test_probe_reports_unreachable(monkeypatch) -> None:
    def fake_create_connection(
        _address: tuple[str, int],
        timeout: float | None = None,
    ) -> DummyConnection:
        raise TimeoutError("timed out")

    monkeypatch.setattr(socket, "create_connection", fake_create_connection)

    result = probe_printer_reachability("ipp://192.168.100.100/ipp/print")

    assert result == {
        "checked": True,
        "host": "192.168.100.100",
        "port": 631,
        "reachable": False,
        "error": "timed out",
    }
