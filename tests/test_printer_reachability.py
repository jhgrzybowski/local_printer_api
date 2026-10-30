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


def test_probe_reports_invalid_device_uri_port_without_connecting(monkeypatch) -> None:
    calls: list[tuple[tuple[str, int], float | None]] = []

    def fake_create_connection(
        address: tuple[str, int],
        timeout: float | None = None,
    ) -> DummyConnection:
        calls.append((address, timeout))
        return DummyConnection()

    monkeypatch.setattr(socket, "create_connection", fake_create_connection)

    result = probe_printer_reachability("ipp://printer.local:bad/ipp/print")

    assert calls == []
    assert result["checked"] is True
    assert result["host"] == "printer.local"
    assert result["port"] is None
    assert result["reachable"] is False
    assert "Invalid printer device URI port" in str(result["error"])


def test_probe_reports_out_of_range_device_uri_port_without_connecting(monkeypatch) -> None:
    calls: list[tuple[tuple[str, int], float | None]] = []

    def fake_create_connection(
        address: tuple[str, int],
        timeout: float | None = None,
    ) -> DummyConnection:
        calls.append((address, timeout))
        return DummyConnection()

    monkeypatch.setattr(socket, "create_connection", fake_create_connection)

    result = probe_printer_reachability("ipp://printer.local:99999/ipp/print")

    assert calls == []
    assert result["checked"] is True
    assert result["host"] == "printer.local"
    assert result["port"] is None
    assert result["reachable"] is False
    assert "Invalid printer device URI port" in str(result["error"])


def test_slow_probe_result_is_reused_while_fresh(monkeypatch) -> None:
    calls: list[str] = []
    clock = [100.0]

    def offline(address: tuple[str, int], timeout: float | None = None) -> DummyConnection:
        calls.append(address[0])
        raise TimeoutError("timed out")

    monkeypatch.setattr(socket, "create_connection", offline)
    monkeypatch.setattr(printer_reachability.time, "monotonic", lambda: clock[0])

    first = probe_printer_reachability("lpd://192.168.100.100/PASSTHRU")
    clock[0] += printer_reachability.PROBE_FRESH_SECONDS - 0.1
    second = probe_printer_reachability("lpd://192.168.100.100/PASSTHRU")

    assert calls == ["192.168.100.100"]
    assert first == second and second["reachable"] is False


def test_stale_probe_answers_at_once_and_refreshes_in_background(monkeypatch) -> None:
    import threading

    clock = [100.0]
    refreshed = threading.Event()
    reachable = [False]

    def probe(address: tuple[str, int], timeout: float | None = None) -> DummyConnection:
        if reachable[0]:
            refreshed.set()
            return DummyConnection()
        raise TimeoutError("timed out")

    monkeypatch.setattr(socket, "create_connection", probe)
    monkeypatch.setattr(printer_reachability.time, "monotonic", lambda: clock[0])

    assert probe_printer_reachability("lpd://192.168.100.100/PASSTHRU")["reachable"] is False
    # The printer is switched on; the next status call must not wait for it.
    reachable[0] = True
    clock[0] += printer_reachability.PROBE_FRESH_SECONDS + 1
    stale = probe_printer_reachability("lpd://192.168.100.100/PASSTHRU")

    assert stale["reachable"] is False
    assert refreshed.wait(2)
    for _ in range(100):
        if probe_printer_reachability("lpd://192.168.100.100/PASSTHRU")["reachable"]:
            break
        threading.Event().wait(0.01)
    assert probe_printer_reachability("lpd://192.168.100.100/PASSTHRU")["reachable"] is True


def test_concurrent_cold_probes_share_one_connection_attempt(monkeypatch) -> None:
    import threading
    from concurrent.futures import ThreadPoolExecutor

    calls: list[str] = []
    started = threading.Event()

    def slow(address: tuple[str, int], timeout: float | None = None) -> DummyConnection:
        calls.append(address[0])
        started.set()
        threading.Event().wait(0.2)
        raise TimeoutError("timed out")

    monkeypatch.setattr(socket, "create_connection", slow)
    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(lambda _: probe_printer_reachability("lpd://10.0.0.9/q"), range(5)))

    assert calls == ["10.0.0.9"]
    assert all(result["reachable"] is False for result in results)


def test_print_submission_requires_a_recent_probe(monkeypatch) -> None:
    clock = [100.0]
    online = [False]
    calls: list[bool] = []

    def probe(address: tuple[str, int], timeout: float | None = None) -> DummyConnection:
        calls.append(online[0])
        if online[0]:
            return DummyConnection()
        raise TimeoutError("timed out")

    monkeypatch.setattr(socket, "create_connection", probe)
    monkeypatch.setattr(printer_reachability.time, "monotonic", lambda: clock[0])
    uri = "lpd://192.168.100.100/PASSTHRU"

    assert probe_printer_reachability(uri)["reachable"] is False
    online[0] = True
    clock[0] += printer_reachability.PROBE_FRESH_SECONDS + 1
    # A print decision does not trust the stale "offline" result.
    assert probe_printer_reachability(uri, max_age=printer_reachability.PROBE_FRESH_SECONDS)["reachable"] is True
    assert calls == [False, True]
