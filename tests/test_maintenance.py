from __future__ import annotations

import os
import time
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app, get_cups_client
from app.services.cups_client import CupsClientError
from app.services.database import Database
from app.services.file_storage import StoredFile, TempFileStorage
from app.services.maintenance import maybe_run_maintenance
from tests.helpers import signup_user
from tests.test_print_api import FakeCupsClient


def old_upload(storage: TempFileStorage, file_id: str) -> None:
    storage.write_record(StoredFile(file_id, "test.pdf", "application/pdf", 4, 1, True, 1))
    storage.file_path(file_id).write_bytes(b"%PDF")
    preview = storage.preview_dir(file_id)
    preview.mkdir()
    (preview / "page-1.png").write_bytes(b"PNG")
    storage.filtered_pdf_path(file_id).write_bytes(b"%PDF")
    old = time.time() - 100 * 86400
    os.utime(storage.metadata_path(file_id), (old, old))


def add_history(database: Database, user_id: int, file_id: str, job_id: int) -> int:
    return database.insert_print_history(
        user_id=user_id, file_id=file_id, original_filename="test.pdf",
        detected_mime="application/pdf", size_bytes=4, page_count=1,
        requested_options={}, applied_options={}, cups_job_id=job_id, warnings=[],
    )


def test_cleanup_respects_live_lease_and_active_job(tmp_path: Path, isolated_database: Database) -> None:
    storage = TempFileStorage(tmp_path / "storage")
    locked_id = "locked-file-123456"
    active_id = "active-file-123456"
    old_upload(storage, locked_id)
    old_upload(storage, active_id)
    user = isolated_database.create_user("user", None, "hash", "salt", 1)
    active_history = add_history(isolated_database, user.id, active_id, 123)
    locked_history = add_history(isolated_database, user.id, locked_id, 999)
    with isolated_database.connect() as connection:
        connection.execute(
            "UPDATE print_history SET created_at = ? WHERE id IN (?, ?)",
            ("2020-01-01T00:00:00+00:00", active_history, locked_history),
        )
    cups = FakeCupsClient()
    with storage.lease(locked_id):
        maybe_run_maintenance(isolated_database, storage, cups, force=True)
        assert storage.file_path(locked_id).exists()
    assert storage.file_path(active_id).exists()
    assert isolated_database.get_print_history(user.id, active_history) is not None
    assert isolated_database.get_print_history(user.id, locked_history) is None
    maybe_run_maintenance(isolated_database, storage, cups, force=True)
    assert not storage.file_path(locked_id).exists()
    assert not storage.metadata_path(locked_id).exists()
    assert not storage.preview_dir(locked_id).exists()
    assert not storage.filtered_pdf_path(locked_id).exists()


def test_cups_outage_defers_destructive_cleanup(tmp_path: Path, isolated_database: Database) -> None:
    storage = TempFileStorage(tmp_path / "storage")
    file_id = "expired-file-123456"
    old_upload(storage, file_id)
    user = isolated_database.create_user("user", None, "hash", "salt", 1)
    isolated_database.create_session(user.id, "token-hash", "2020-01-01T00:00:00+00:00", None, None)
    cups = FakeCupsClient()
    def unavailable(scope: str = "active") -> list[dict[str, object]]:
        raise CupsClientError("unavailable")
    cups.list_jobs = unavailable  # type: ignore[method-assign]
    maybe_run_maintenance(isolated_database, storage, cups, force=True)
    assert storage.file_path(file_id).exists()
    with isolated_database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


def test_history_is_paginated_per_user(isolated_database: Database) -> None:
    cups = FakeCupsClient()
    app.dependency_overrides[get_cups_client] = lambda: cups
    try:
        with TestClient(app) as client:
            user_id = signup_user(client)["user"]["id"]
            ids = [add_history(isolated_database, user_id, f"file-{i}", 100 + i) for i in range(4)]
            first = client.get("/history?limit=2&offset=0")
            second = client.get("/history?limit=2&offset=2")
            assert first.status_code == 200
            assert [row["id"] for row in first.json()["history"]] == ids[3:1:-1]
            assert [row["id"] for row in second.json()["history"]] == ids[1::-1]
            assert first.json()["total"] == 4
            assert first.json()["limit"] == 2
            assert second.json()["offset"] == 2
            assert client.get("/history?limit=101").status_code == 422
    finally:
        app.dependency_overrides.clear()
