from __future__ import annotations

import os
import time
import fcntl
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest
from fastapi.testclient import TestClient

from app.main import app, get_cups_client, get_file_storage
from app.services.cups_client import CupsClientError
from app.services.database import Database, JobClaim
from app.services.file_storage import StorageError, StoredFile, TempFileStorage
from app.services.maintenance import maybe_run_maintenance
from tests.helpers import signup_user
from tests.test_print_api import FakeCupsClient, make_pdf


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


def test_cleanup_during_history_insert_keeps_submitted_upload(
    tmp_path: Path, isolated_database: Database, monkeypatch,
) -> None:
    storage = TempFileStorage(tmp_path / "storage")
    cups = FakeCupsClient()
    app.dependency_overrides[get_file_storage] = lambda: storage
    app.dependency_overrides[get_cups_client] = lambda: cups
    try:
        with TestClient(app) as client:
            signup_user(client)
            upload = client.post(
                "/files", files={"file": ("old.pdf", make_pdf(1), "application/pdf")},
            )
            file_id = upload.json()["file_id"]
            old = time.time() - 100 * 86400
            os.utime(storage.metadata_path(file_id), (old, old))
            insert = isolated_database.insert_print_history

            def cleanup_before_insert(*args, **kwargs):
                # CUPS exposes the new job, but history has no mapping yet.
                maybe_run_maintenance(isolated_database, storage, cups, force=True)
                assert storage.file_path(file_id).exists()
                return insert(*args, **kwargs)

            monkeypatch.setattr(isolated_database, "insert_print_history", cleanup_before_insert)
            response = client.post("/print", json={"file_id": file_id, "options": {}})
            assert response.status_code == 200
            assert response.json()["history_id"] is not None
            assert storage.file_path(file_id).exists()
            maybe_run_maintenance(isolated_database, storage, cups, force=True)
            assert storage.file_path(file_id).exists()
    finally:
        app.dependency_overrides.clear()


def test_cleanup_rechecks_job_mapping_after_initial_snapshot(
    tmp_path: Path, isolated_database: Database, monkeypatch,
) -> None:
    storage = TempFileStorage(tmp_path / "storage")
    file_id = "newly-printed-file"
    old_upload(storage, file_id)
    user = isolated_database.create_user("owner", None, "hash", "salt", 1)
    original_prune = storage.prune_expired

    def persist_print_before_prune(*args, **kwargs):
        # The initial active-job snapshot saw no file mapping; /print then
        # persisted one and released its lease before this cleanup pass.
        add_history(isolated_database, user.id, file_id, 123)
        return original_prune(*args, **kwargs)

    monkeypatch.setattr(storage, "prune_expired", persist_print_before_prune)
    maybe_run_maintenance(isolated_database, storage, FakeCupsClient(), force=True)

    assert storage.file_path(file_id).exists()
    assert storage.metadata_path(file_id).exists()


def test_cleanup_keeps_filtered_pdf_while_source_is_leased(tmp_path: Path) -> None:
    storage = TempFileStorage(tmp_path / "storage")
    file_id = "leased-filtered-file"
    old_upload(storage, file_id)
    filtered = next(storage.filtered_dir.glob(f"{file_id}-*.pdf"))
    old = time.time() - 100 * 86400
    os.utime(filtered, (old, old))

    with storage.lease(file_id):
        storage.prune_expired(time.time() - 7 * 86400, set())
        assert filtered.exists()
        filtered.write_bytes(b"%PDF fresh")

    assert filtered.read_bytes() == b"%PDF fresh"


def test_lease_rejects_file_removed_while_waiting_for_cleanup_lock(
    tmp_path: Path, monkeypatch,
) -> None:
    storage = TempFileStorage(tmp_path / "storage")
    file_id = "removed-while-waiting"
    old_upload(storage, file_id)
    attempted = Event()
    original_flock = fcntl.flock

    def signal_shared_lock(fd, operation):
        if operation == fcntl.LOCK_SH:
            attempted.set()
        return original_flock(fd, operation)

    def acquire_lease() -> None:
        with storage.lease(file_id):
            pass

    with storage.file_path(file_id).open("rb") as cleanup_source:
        original_flock(cleanup_source, fcntl.LOCK_EX)
        monkeypatch.setattr(fcntl, "flock", signal_shared_lock)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(acquire_lease)
            assert attempted.wait(timeout=5)
            storage.metadata_path(file_id).unlink()
            storage.file_path(file_id).unlink()
            original_flock(cleanup_source, fcntl.LOCK_UN)
            with pytest.raises(StorageError, match="Stored file is missing"):
                future.result(timeout=5)


def test_fallback_claim_protects_source_upload(
    tmp_path: Path, isolated_database: Database,
) -> None:
    storage = TempFileStorage(tmp_path / "storage")
    file_id = "fallback-file-123456"
    old_upload(storage, file_id)
    user = isolated_database.create_user("owner", None, "hash", "salt", 1)
    claim = JobClaim(user.id, 123, "ipp://localhost/printers/Canon_MG5350", 1123, "urn:uuid:job-123")
    isolated_database.save_fallback_job_claim(claim, user.identity_id, file_id=file_id)
    assert isolated_database.list_fallback_job_claims(user.id) == [claim]
    maybe_run_maintenance(isolated_database, storage, FakeCupsClient(), force=True)
    assert storage.file_path(file_id).exists()


def test_old_fallback_claims_expire_except_active_jobs(
    tmp_path: Path, isolated_database: Database,
) -> None:
    storage = TempFileStorage(tmp_path / "storage")
    user = isolated_database.create_user("owner", None, "hash", "salt", 1)
    uri = "ipp://localhost/printers/Canon_MG5350"
    expired = JobClaim(user.id, 999, uri, 1000, "urn:uuid:expired")
    active = JobClaim(user.id, 123, uri, 1001, "urn:uuid:active")
    isolated_database.save_fallback_job_claim(expired, user.identity_id)
    isolated_database.save_fallback_job_claim(active, user.identity_id)
    directory = isolated_database.path.parent / "job-claims" / isolated_database.database_id()
    old = time.time() - 100 * 86400
    for path in directory.glob("*.json"):
        os.utime(path, (old, old))

    maybe_run_maintenance(isolated_database, storage, FakeCupsClient(), force=True)

    assert isolated_database.list_fallback_job_claims(user.id) == [active]


def test_maintenance_removes_purge_marker_with_expired_history(
    tmp_path: Path, isolated_database: Database,
) -> None:
    storage = TempFileStorage(tmp_path / "storage")
    user = isolated_database.create_user("owner", None, "hash", "salt", 1)
    claim = JobClaim(user.id, 999, "ipp://localhost/printers/Canon_MG5350", 1999, "urn:uuid:job-999")
    history_id = isolated_database.insert_print_history(
        user_id=user.id, file_id="expired-history", original_filename="old.pdf",
        detected_mime="application/pdf", size_bytes=4, page_count=1,
        requested_options={}, applied_options={}, cups_job_id=999,
        warnings=[], job_claim=claim,
    )
    with isolated_database.connect() as connection:
        connection.execute(
            "UPDATE print_history SET created_at = ? WHERE id = ?",
            ("2020-01-01T00:00:00+00:00", history_id),
        )
    isolated_database.save_forgotten_job_marker(claim)
    marker = isolated_database.forgotten_job_marker_path(claim)
    assert marker.exists()

    maybe_run_maintenance(isolated_database, storage, FakeCupsClient(), force=True)

    assert isolated_database.get_print_history(user.id, history_id) is None
    assert not marker.exists()


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
