from __future__ import annotations

from collections.abc import Mapping
from typing import Any


CUPS_STATE_LABELS = {
    3: "idle",
    4: "processing",
    5: "stopped",
}

OFFLINE_REASON_TOKENS = (
    "offline",
    "not-connected",
    "not connected",
    "unreachable",
    "connection-failed",
    "connection failed",
    "timed-out",
    "timeout",
)


def translate_queue_status(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    queue_name = str(snapshot.get("name") or "")
    exists = bool(snapshot.get("exists"))
    attributes = snapshot.get("attributes") or {}

    if not isinstance(attributes, Mapping):
        attributes = {}

    if not exists:
        return {
            "queue_name": queue_name,
            "exists": False,
            "state": "missing",
            "state_code": None,
            "accepting_jobs": None,
            "enabled": False,
            "message": "",
            "device_uri": None,
            "location": None,
            "reasons": [],
        }

    state_code = _as_int(attributes.get("printer-state"))
    accepting_jobs = _as_bool(attributes.get("printer-is-accepting-jobs"))
    reasons = _as_list(attributes.get("printer-state-reasons"))
    state = (
        "offline"
        if _has_offline_reason(reasons)
        else CUPS_STATE_LABELS.get(state_code, "unknown")
    )

    return {
        "queue_name": queue_name,
        "exists": True,
        "state": state,
        "state_code": state_code,
        "accepting_jobs": accepting_jobs,
        "enabled": state_code != 5 if state_code is not None else None,
        "message": str(attributes.get("printer-state-message") or ""),
        "device_uri": attributes.get("device-uri"),
        "location": attributes.get("printer-location"),
        "reasons": reasons,
    }


def translate_error_status(queue_name: str, error: str) -> dict[str, Any]:
    return {
        "queue_name": queue_name,
        "exists": False,
        "state": "unknown",
        "state_code": None,
        "accepting_jobs": None,
        "enabled": None,
        "message": error,
        "device_uri": None,
        "location": None,
        "reasons": [],
    }


def _has_offline_reason(reasons: list[str]) -> bool:
    normalized_reasons = [
        reason.strip().lower().replace("_", "-") for reason in reasons
    ]
    return any(
        token in reason
        for reason in normalized_reasons
        for token in OFFLINE_REASON_TOKENS
    )


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "yes", "1"}:
            return True
        if lowered in {"false", "no", "0"}:
            return False
    return None


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list | tuple | set):
        return [str(item) for item in value]
    return [str(value)]
