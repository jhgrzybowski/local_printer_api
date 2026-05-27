from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from app.settings import QUEUE_NAME
from app.services.lpoptions_parser import parse_lpoptions, parse_ppd_options
from app.services.printer_reachability import probe_printer_reachability


class CupsClientError(RuntimeError):
    """Raised when CUPS cannot be reached or queried."""


JOB_SCOPE_TO_CUPS = {
    "active": "not-completed",
    "completed": "completed",
    "all": "all",
}
JOB_REQUESTED_ATTRIBUTES = ["all"]

ACTIVE_JOB_STATES = {3, 4, 5, 6}
TERMINAL_JOB_STATES = {7, 8, 9}


class CupsClient:
    """Small lazy wrapper around the CUPS Python bindings."""

    def __init__(self, queue_name: str = QUEUE_NAME) -> None:
        self.queue_name = queue_name

    def _connection(self) -> Any:
        try:
            import cups  # type: ignore[import-not-found]
        except ImportError as exc:
            raise CupsClientError("CUPS Python bindings are not installed") from exc

        try:
            return cups.Connection()
        except Exception as exc:
            raise CupsClientError(f"CUPS connection failed: {exc}") from exc

    def get_queue(self) -> dict[str, Any]:
        try:
            connection = self._connection()
            printers = connection.getPrinters()
        except Exception as exc:
            raise CupsClientError(f"CUPS query failed: {exc}") from exc

        if self.queue_name not in printers:
            return {"name": self.queue_name, "exists": False, "attributes": {}}

        attributes = dict(printers[self.queue_name])
        return {
            "name": self.queue_name,
            "exists": True,
            "attributes": attributes,
            "network": probe_printer_reachability(attributes.get("device-uri")),
        }

    def get_option_capabilities(self) -> dict[str, set[str]]:
        lpoptions_capabilities = self._get_lpoptions_capabilities()
        if lpoptions_capabilities:
            return lpoptions_capabilities
        return self._get_ppd_capabilities()

    def _get_lpoptions_capabilities(self) -> dict[str, set[str]]:
        try:
            completed = subprocess.run(
                ["lpoptions", "-p", self.queue_name, "-l"],
                check=False,
                capture_output=True,
                text=True,
                timeout=8,
            )
        except (OSError, subprocess.TimeoutExpired):
            return {}

        if completed.returncode != 0:
            return {}
        return parse_lpoptions(completed.stdout)

    def _get_ppd_capabilities(self) -> dict[str, set[str]]:
        try:
            connection = self._connection()
            ppd_path = Path(connection.getPPD(self.queue_name))
            return parse_ppd_options(ppd_path.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            return {}

    def print_file(self, path: Path, title: str, options: dict[str, str]) -> int:
        try:
            connection = self._connection()
            return int(connection.printFile(self.queue_name, str(path), title, options))
        except Exception as exc:
            raise CupsClientError(f"CUPS print submission failed: {exc}") from exc

    def list_jobs(self, scope: str = "active") -> list[dict[str, Any]]:
        which_jobs = JOB_SCOPE_TO_CUPS[scope]
        try:
            connection = self._connection()
            jobs = connection.getJobs(
                which_jobs=which_jobs,
                requested_attributes=JOB_REQUESTED_ATTRIBUTES,
            )
        except Exception as exc:
            raise CupsClientError(f"CUPS job query failed: {exc}") from exc
        return [normalize_job(job_id, attrs) for job_id, attrs in jobs.items()]

    def job_counts(self) -> dict[str, int]:
        return {scope: len(self.list_jobs(scope)) for scope in JOB_SCOPE_TO_CUPS}

    def get_job(self, job_id: int) -> dict[str, Any] | None:
        try:
            connection = self._connection()
            attrs = connection.getJobAttributes(job_id)
        except Exception as exc:
            message = str(exc).lower()
            if "not found" in message or "no such" in message or "unknown" in message:
                return None
            raise CupsClientError(f"CUPS job query failed: {exc}") from exc
        return normalize_job(job_id, attrs)

    def cancel_job(self, job_id: int) -> dict[str, Any]:
        job = self.get_job(job_id)
        if job is None:
            return {
                "job_id": job_id,
                "cancelled": False,
                "already_terminal": False,
                "can_forget": False,
                "message": "Job was not found.",
            }
        if not job["can_cancel"]:
            return cancel_not_possible_response(job)

        try:
            connection = self._connection()
            connection.cancelJob(job_id)
        except Exception as exc:
            message = str(exc).lower()
            if "not found" in message or "no such" in message or "unknown" in message:
                return {
                    "job_id": job_id,
                    "cancelled": False,
                    "already_terminal": False,
                    "can_forget": False,
                    "message": "Job was not found.",
                }
            if is_not_possible_error(message):
                refreshed = self.get_job(job_id)
                if refreshed is not None:
                    return cancel_not_possible_response(refreshed)
                return {
                    "job_id": job_id,
                    "cancelled": False,
                    "already_terminal": False,
                    "can_forget": False,
                    "message": "CUPS says this job cannot be cancelled.",
                }
            raise CupsClientError(f"CUPS job cancellation failed: {exc}") from exc
        return {
            "job_id": job_id,
            "cancelled": True,
            "already_terminal": False,
            "can_forget": False,
            "message": "Job cancellation was submitted.",
        }

    def forget_job(self, job_id: int) -> dict[str, Any]:
        job = self.get_job(job_id)
        if job is None:
            return {
                "job_id": job_id,
                "forgotten": False,
                "method": "pycups-purge-job",
                "reason": "Job was not found.",
            }
        if job["can_cancel"]:
            return {
                "job_id": job_id,
                "forgotten": False,
                "method": "pycups-purge-job",
                "reason": "Job is still active; cancel it before purging history.",
            }

        try:
            connection = self._connection()
            connection.cancelJob(job_id, purge_job=True)
        except TypeError as exc:
            return {
                "job_id": job_id,
                "forgotten": False,
                "method": "pycups-purge-job",
                "reason": f"Installed pycups does not support purge_job: {exc}",
            }
        except Exception as exc:
            message = str(exc).lower()
            if "not found" in message or "no such" in message or "unknown" in message:
                return {
                    "job_id": job_id,
                    "forgotten": False,
                    "method": "pycups-purge-job",
                    "reason": "Job was not found.",
                }
            return {
                "job_id": job_id,
                "forgotten": False,
                "method": "pycups-purge-job",
                "reason": f"CUPS does not allow purging this job with current permissions: {exc}",
            }

        return {"job_id": job_id, "forgotten": True, "method": "pycups-purge-job"}


JOB_STATE_LABELS = {
    3: "pending",
    4: "pending-held",
    5: "processing",
    6: "processing-stopped",
    7: "canceled",
    8: "aborted",
    9: "completed",
}


def normalize_job(job_id: int, attrs: dict[str, Any]) -> dict[str, Any]:
    state_code = _as_int(attrs.get("job-state"))
    is_active = state_code in ACTIVE_JOB_STATES
    is_terminal = state_code in TERMINAL_JOB_STATES
    can_cancel = is_active
    can_forget = is_terminal
    return {
        "job_id": int(job_id),
        "name": attrs.get("job-name") or attrs.get("document-name-supplied") or "",
        "user": attrs.get("job-originating-user-name") or "",
        "state": JOB_STATE_LABELS.get(state_code, "unknown"),
        "state_code": state_code,
        "reasons": _as_list(attrs.get("job-state-reasons")),
        "printer_uri": attrs.get("job-printer-uri"),
        "created_at": attrs.get("time-at-creation"),
        "completed_at": attrs.get("time-at-completed"),
        "is_active": is_active,
        "is_terminal": is_terminal,
        "can_cancel": can_cancel,
        "can_forget": can_forget,
        "action_hint": action_hint(state_code),
    }


def cancel_not_possible_response(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "job_id": job["job_id"],
        "cancelled": False,
        "already_terminal": job["is_terminal"],
        "can_forget": job["can_forget"],
        "message": "Job is already completed/canceled/aborted and cannot be cancelled."
        if job["is_terminal"]
        else "CUPS says this job cannot be cancelled.",
    }


def is_not_possible_error(message: str) -> bool:
    return "client-error-not-possible" in message or "already completed" in message


def action_hint(state_code: int | None) -> str:
    if state_code in ACTIVE_JOB_STATES:
        return "This job is active and can be cancelled."
    if state_code in TERMINAL_JOB_STATES:
        return (
            "This job is historical and cannot be cancelled. "
            "It can only be hidden or purged if supported."
        )
    return "This job state is unknown; it is not assumed to be cancelable."


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


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list | tuple | set):
        return [str(item) for item in value]
    return [str(value)]
