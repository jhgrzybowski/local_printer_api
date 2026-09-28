from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone

from app.services.cups_client import CupsClient, CupsClientError
from app.services.database import Database
from app.services.file_storage import TempFileStorage
from app.settings import HISTORY_TTL_DAYS, UPLOAD_TTL_DAYS


LOGGER = logging.getLogger(__name__)
_lock = threading.Lock()
_last_run: dict[tuple[str, str], float] = {}
INTERVAL_SECONDS = 3600


def maybe_run_maintenance(
    database: Database, storage: TempFileStorage, client: CupsClient,
    *, force: bool = False,
) -> None:
    """Run bounded, synchronous housekeeping at most once an hour per process."""
    key = (str(database.path), str(storage.root))
    with _lock:
        now = time.monotonic()
        if not force and now - _last_run.get(key, -INTERVAL_SECONDS) < INTERVAL_SECONDS:
            return
        _last_run[key] = now

    try:
        database.delete_expired_sessions()
        # Without an active-job snapshot, deleting source files or claims is unsafe.
        active_jobs = client.list_jobs("active")
        active_ids = {int(job["job_id"]) for job in active_jobs}
        protected_files = database.file_ids_for_jobs(active_ids)
        def is_protected_now(file_id: str) -> bool:
            # Called with the source's exclusive lock held. A print cannot
            # start until this check and any deletion have completed.
            current_ids = {int(job["job_id"]) for job in client.list_jobs("active")}
            return file_id in database.file_ids_for_jobs(current_ids)
        cutoff = datetime.now(timezone.utc) - timedelta(days=UPLOAD_TTL_DAYS)
        storage.prune_expired(cutoff.timestamp(), protected_files, is_protected_now)
        history_cutoff = (
            datetime.now(timezone.utc) - timedelta(days=HISTORY_TTL_DAYS)
        ).replace(microsecond=0).isoformat()
        database.prune_print_history(history_cutoff, active_ids)
        database.prune_fallback_job_claims(
            (datetime.now(timezone.utc) - timedelta(days=HISTORY_TTL_DAYS)).timestamp(),
            active_ids,
        )
    except CupsClientError as exc:
        LOGGER.warning("CUPS unavailable; deferred file and history cleanup: %s", exc)
    except Exception:
        LOGGER.exception("Maintenance failed; will retry on the next interval")
