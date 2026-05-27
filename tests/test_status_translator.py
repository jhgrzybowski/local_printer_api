from app.services.status_translator import translate_error_status, translate_queue_status


def test_translate_existing_idle_queue() -> None:
    status = translate_queue_status(
        {
            "name": "Canon_MG5350",
            "exists": True,
            "attributes": {
                "printer-state": 3,
                "printer-is-accepting-jobs": True,
                "printer-state-message": "ready",
                "device-uri": "ipp://192.168.100.100/ipp/print",
                "printer-location": "Office",
                "printer-state-reasons": ["none"],
            },
        }
    )

    assert status == {
        "queue_name": "Canon_MG5350",
        "exists": True,
        "state": "idle",
        "state_code": 3,
        "accepting_jobs": True,
        "enabled": True,
        "message": "ready",
        "device_uri": "ipp://192.168.100.100/ipp/print",
        "location": "Office",
        "reasons": ["none"],
    }


def test_translate_missing_queue() -> None:
    status = translate_queue_status(
        {"name": "Canon_MG5350", "exists": False, "attributes": {}}
    )

    assert status["queue_name"] == "Canon_MG5350"
    assert status["exists"] is False
    assert status["state"] == "missing"
    assert status["state_code"] is None
    assert status["accepting_jobs"] is None
    assert status["enabled"] is False


def test_translate_stopped_queue_with_string_values() -> None:
    status = translate_queue_status(
        {
            "name": "Canon_MG5350",
            "exists": True,
            "attributes": {
                "printer-state": "5",
                "printer-is-accepting-jobs": "false",
                "printer-state-reasons": "paused",
            },
        }
    )

    assert status["state"] == "stopped"
    assert status["state_code"] == 5
    assert status["accepting_jobs"] is False
    assert status["enabled"] is False
    assert status["reasons"] == ["paused"]


def test_translate_offline_reason_reports_offline_state() -> None:
    status = translate_queue_status(
        {
            "name": "Canon_MG5350",
            "exists": True,
            "attributes": {
                "printer-state": 3,
                "printer-is-accepting-jobs": True,
                "printer-state-reasons": ["offline-report"],
            },
        }
    )

    assert status["state"] == "offline"
    assert status["state_code"] == 3
    assert status["accepting_jobs"] is True
    assert status["enabled"] is True
    assert status["reasons"] == ["offline-report"]


def test_translate_error_status() -> None:
    status = translate_error_status("Canon_MG5350", "CUPS query failed")

    assert status["queue_name"] == "Canon_MG5350"
    assert status["exists"] is False
    assert status["state"] == "unknown"
    assert status["message"] == "CUPS query failed"
