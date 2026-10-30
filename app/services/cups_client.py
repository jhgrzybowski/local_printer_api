from __future__ import annotations

import logging
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from app.settings import QUEUE_NAME
from app.services.lpoptions_parser import parse_lpoptions
from app.services.ppd import PpdInfo, parse_ppd
from app.services.printer_reachability import probe_printer_reachability


class CupsClientError(RuntimeError):
    """Raised when CUPS cannot be reached or queried."""


class CupsJobChangedError(CupsClientError):
    """The numeric job ID no longer identifies the authorized CUPS job."""


JOB_SCOPE_TO_CUPS = {
    "active": "not-completed",
    "completed": "completed",
    "all": "all",
}
# Only what normalize_job reads. Asking for "all" makes CUPS encode and pycups
# decode every stored attribute of every retained job on each poll.
JOB_REQUESTED_ATTRIBUTES = [
    "job-id",
    "job-name",
    "document-name-supplied",
    "job-originating-user-name",
    "job-state",
    "job-state-reasons",
    "job-printer-uri",
    "time-at-creation",
    "time-at-completed",
    "job-uuid",
    "time-at-processing",
    "job-impressions-completed",
    "job-media-sheets-completed",
    "job-printer-state-message",
    "job-media-progress",
]

ACTIVE_JOB_STATES = {3, 4, 5, 6}
TERMINAL_JOB_STATES = {7, 8, 9}
LOGGER = logging.getLogger(__name__)

# Printer capabilities change only when an administrator edits the queue.
# Revalidate after this many seconds; an unchanged PPD costs one 304 reply.
CAPABILITY_CACHE_SECONDS = 30
# Standard IPP job attributes that CUPS applies through its own filters and
# PPD presets. They are not PPD options, so lpoptions never lists them.
IPP_CAPABILITY_ATTRIBUTES = [
    "sides-supported",
    "print-color-mode-supported",
    "print-quality-supported",
    "number-up-supported",
    "number-up-layout-supported",
    "page-delivery-supported",
    "print-scaling-supported",
    "orientation-requested-supported",
    "media-source-supported",
    "media-supported",
    "multiple-document-handling-supported",
    "sides-default",
    "print-color-mode-default",
    "print-quality-default",
]


@dataclass(frozen=True)
class PrinterProfile:
    """Everything a print dialog needs to know about the queue."""

    # Option keyword -> accepted values. PPD options plus the standard IPP job
    # attributes CUPS advertises, which the option mapper prefers when present.
    capabilities: Mapping[str, set[str]]
    ppd: PpdInfo | None = None
    ipp: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class _CachedProfile:
    profile: PrinterProfile
    checked_at: float
    ppd_modtime: float | None


_profile_cache: dict[str, _CachedProfile] = {}
_profile_lock = threading.Lock()


def clear_profile_cache() -> None:
    with _profile_lock:
        _profile_cache.clear()


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

    def get_queue(self, probe_max_age: float | None = None) -> dict[str, Any]:
        try:
            connection = self._connection()
            printers = connection.getPrinters()
        except Exception as exc:
            raise CupsClientError(f"CUPS query failed: {exc}") from exc

        if self.queue_name not in printers:
            return {"name": self.queue_name, "exists": False, "attributes": {}}

        attributes = dict(printers[self.queue_name])
        printer_uris = attributes.get("printer-uri-supported")
        if isinstance(printer_uris, str):
            printer_uri = printer_uris
        elif isinstance(printer_uris, list | tuple):
            printer_uri = next(
                (uri for uri in printer_uris if isinstance(uri, str) and uri),
                None,
            )
        else:
            printer_uri = None

        # CUPS getPrinters() omits this attribute on some versions. Query it
        # explicitly because print readiness must distinguish false from unknown.
        if printer_uri:
            try:
                attributes.update(
                    connection.getPrinterAttributes(
                        uri=printer_uri,
                        requested_attributes=["printer-is-accepting-jobs"],
                    )
                )
            except Exception as exc:
                # Keep readiness unknown if CUPS cannot provide the attribute.
                LOGGER.warning(
                    "Could not query printer-is-accepting-jobs for %s: %s",
                    self.queue_name,
                    exc,
                )

        return {
            "name": self.queue_name,
            "exists": True,
            "attributes": attributes,
            "network": probe_printer_reachability(attributes.get("device-uri"), max_age=probe_max_age),
        }

    def get_option_capabilities(self) -> dict[str, set[str]]:
        return {name: set(values) for name, values in self.get_printer_profile().capabilities.items()}

    def get_printer_profile(self) -> PrinterProfile:
        """Return cached queue capabilities, revalidating them periodically.

        A print dialog validates every settings change. Spawning lpoptions and
        reparsing a 100 KB PPD each time cost more than the rest of the request.
        """
        with _profile_lock:
            cached = _profile_cache.get(self.queue_name)
            if cached is not None and time.monotonic() - cached.checked_at < CAPABILITY_CACHE_SECONDS:
                return cached.profile
            try:
                profile, ppd_modtime = self._load_profile(cached)
            except CupsClientError:
                _profile_cache.pop(self.queue_name, None)
                raise
            _profile_cache[self.queue_name] = _CachedProfile(profile, time.monotonic(), ppd_modtime)
            return profile

    def _load_profile(self, cached: _CachedProfile | None) -> tuple[PrinterProfile, float | None]:
        connection = self._connection()
        # The attribute request doubles as the existence check: lpoptions can
        # fail for a missing queue without saying whether CUPS is available.
        try:
            ipp = dict(connection.getPrinterAttributes(
                self.queue_name, requested_attributes=IPP_CAPABILITY_ATTRIBUTES,
            ))
        except Exception as exc:
            if _is_not_found(exc):
                raise CupsClientError(f"CUPS queue {self.queue_name} does not exist") from exc
            raise CupsClientError(f"CUPS queue query failed: {exc}") from exc

        errors: list[str] = []
        ppd: PpdInfo | None = None
        ppd_modtime: float | None = None
        try:
            ppd, ppd_modtime = self._fetch_ppd(connection, cached)
        except CupsClientError as exc:
            errors.append(str(exc))
        capabilities: dict[str, set[str]] = ppd.capabilities() if ppd is not None else {}
        if not capabilities:
            try:
                capabilities = self._get_lpoptions_capabilities()
            except CupsClientError as exc:
                errors.append(str(exc))
        capabilities.update(ipp_job_capabilities(ipp))
        if not capabilities:
            raise CupsClientError("CUPS capability detection failed: " + "; ".join(errors or ["no options"]))
        return PrinterProfile(capabilities, ppd, ipp), ppd_modtime

    def _fetch_ppd(self, connection: Any, cached: _CachedProfile | None) -> tuple[PpdInfo | None, float | None]:
        previous_modtime = cached.ppd_modtime if cached is not None and cached.profile.ppd else None
        try:
            if previous_modtime is None:
                status, modtime, filename = connection.getPPD3(self.queue_name)
            else:
                status, modtime, filename = connection.getPPD3(self.queue_name, modtime=previous_modtime)
        except Exception as exc:
            raise CupsClientError(f"CUPS PPD query failed: {exc}") from exc
        try:
            if status == 304 and cached is not None and cached.profile.ppd is not None:
                return cached.profile.ppd, previous_modtime
            if status != 200 or not filename:
                raise CupsClientError(f"CUPS PPD query failed: HTTP {status}")
            text = Path(filename).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise CupsClientError(f"CUPS PPD query failed: {exc}") from exc
        finally:
            # pycups writes a temporary copy even for a 304 reply.
            if filename:
                Path(filename).unlink(missing_ok=True)
        ppd = parse_ppd(text)
        return (ppd if ppd.options else None), modtime

    def _get_lpoptions_capabilities(self) -> dict[str, set[str]]:
        try:
            completed = subprocess.run(
                ["lpoptions", "-p", self.queue_name, "-l"],
                check=False,
                capture_output=True,
                text=True,
                timeout=8,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CupsClientError(f"lpoptions failed: {exc}") from exc

        if completed.returncode != 0:
            raise CupsClientError(f"lpoptions failed: {completed.stderr.strip() or completed.returncode}")
        return parse_lpoptions(completed.stdout)

    def print_file(self, path: Path, title: str, options: dict[str, str]) -> int:
        try:
            connection = self._connection()
            return int(connection.printFile(self.queue_name, str(path), title, options))
        except Exception as exc:
            raise CupsClientError(f"CUPS print submission failed: {exc}") from exc

    def list_jobs(self, scope: str = "active", first_job_id: int | None = None) -> list[dict[str, Any]]:
        which_jobs = JOB_SCOPE_TO_CUPS[scope]
        try:
            connection = self._connection()
            jobs = connection.getJobs(
                which_jobs=which_jobs,
                requested_attributes=JOB_REQUESTED_ATTRIBUTES,
                first_job_id=-1 if first_job_id is None else first_job_id,
            )
        except Exception as exc:
            raise CupsClientError(f"CUPS job query failed: {exc}") from exc

        normalized_jobs: list[dict[str, Any]] = []
        for job_id, attrs in jobs.items():
            job = normalize_job(job_id, attrs)
            if not job["job_uuid"]:
                try:
                    detailed = self._get_job_from_connection(connection, int(job_id))
                except CupsClientError as exc:
                    LOGGER.warning("Could not load identity for CUPS job %s: %s", job_id, exc)
                    detailed = None
                if (
                    detailed is not None
                    and detailed["printer_uri"] == job["printer_uri"]
                    and detailed["created_at"] == job["created_at"]
                    and detailed["state_code"] == job["state_code"]
                ):
                    job = detailed
            normalized_jobs.append(job)
        return normalized_jobs

    def get_job(self, job_id: int) -> dict[str, Any] | None:
        connection = self._connection()
        return self._get_job_from_connection(connection, job_id)

    def _get_job_from_connection(self, connection: Any, job_id: int) -> dict[str, Any] | None:
        try:
            attrs = connection.getJobAttributes(job_id)
        except Exception as exc:
            message = str(exc).lower()
            if "not found" in message or "no such" in message or "unknown" in message:
                return None
            raise CupsClientError(f"CUPS job query failed: {exc}") from exc
        return normalize_job(job_id, attrs)

    def cancel_job(
        self, job_id: int, expected_job: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        connection = self._connection()
        job = self._get_job_from_connection(connection, job_id)
        self._require_same_job(expected_job, job)
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

    def forget_job(
        self, job_id: int, expected_job: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        connection = self._connection()
        job = self._get_job_from_connection(connection, job_id)
        self._require_same_job(expected_job, job)
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

    @staticmethod
    def _require_same_job(
        expected_job: dict[str, Any] | None, current_job: dict[str, Any] | None,
    ) -> None:
        if expected_job is None:
            return
        identity_fields = ("job_id", "printer_uri", "created_at", "job_uuid")
        if current_job is None or any(
            expected_job.get(field) != current_job.get(field)
            for field in identity_fields
        ):
            raise CupsJobChangedError("CUPS job identity changed before the action")


JOB_STATE_LABELS = {
    3: "pending",
    4: "pending-held",
    5: "processing",
    6: "processing-stopped",
    7: "canceled",
    8: "aborted",
    9: "completed",
}


def ipp_job_capabilities(ipp: Mapping[str, Any]) -> dict[str, set[str]]:
    """Map advertised IPP *-supported attributes to job option values."""
    capabilities: dict[str, set[str]] = {}
    for option in (
        "sides", "print-color-mode", "print-quality", "number-up", "number-up-layout",
        "page-delivery", "print-scaling", "orientation-requested", "media-source", "media",
    ):
        values = _as_list(ipp.get(f"{option}-supported"))
        if values:
            capabilities[option] = set(values)
    # pdftopdf collates copies itself when the queue advertises collated copies.
    if "separate-documents-collated-copies" in _as_list(ipp.get("multiple-document-handling-supported")):
        capabilities["collate"] = {"true", "false"}
    return capabilities


def _is_not_found(exc: Exception) -> bool:
    status = exc.args[0] if exc.args else None
    return status == 0x0406 or "not-found" in str(exc).lower() or "does not exist" in str(exc).lower()


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
        "job_uuid": attrs.get("job-uuid"),
        "completed_at": attrs.get("time-at-completed"),
        "processing_at": attrs.get("time-at-processing"),
        # Live progress for a print dialog: pages and sheets finished, and how
        # far CUPS has rendered the current sheet (a CUPS extension, 0-100).
        "pages_completed": _as_int(attrs.get("job-impressions-completed")),
        "sheets_completed": _as_int(attrs.get("job-media-sheets-completed")),
        "media_progress": _as_int(attrs.get("job-media-progress")),
        "state_message": attrs.get("job-printer-state-message") or "",
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
