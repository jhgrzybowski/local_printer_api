from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app, get_file_storage
from app.services.database import Database
from app.services.file_storage import LegacyClaimError, TempFileStorage
from scripts.claim_legacy_upload import main as claim_main
from tests.helpers import signup_user


FILE_ID = "legacyUploadId123456789"


def create_legacy_upload(storage: TempFileStorage) -> None:
    storage._ensure_dirs()
    storage.file_path(FILE_ID).write_bytes(b"legacy text\n")
    storage.metadata_path(FILE_ID).write_text(
        json.dumps({
            "file_id": FILE_ID,
            "original_filename": "legacy.txt",
            "detected_mime": "text/plain",
            "size_bytes": 12,
            "page_count": None,
            "preview_available": False,
        }),
        encoding="utf-8",
    )


def test_local_claim_is_explicit_and_keeps_other_accounts_out(
    tmp_path: Path, isolated_database: Database, capsys: pytest.CaptureFixture[str],
) -> None:
    storage = TempFileStorage(tmp_path / "uploads")
    create_legacy_upload(storage)
    app.dependency_overrides.clear()
    app.dependency_overrides[get_file_storage] = lambda: storage
    command_prefix = ["--tmp-dir", str(storage.root), "--db-path", str(isolated_database.path)]

    try:
        with TestClient(app) as alice, TestClient(app) as bob:
            alice_id = signup_user(alice, username="alice")["user"]["id"]
            signup_user(bob, username="bob")

            assert storage.get_record(FILE_ID).owner_user_id is None
            assert alice.post("/print", json={"file_id": FILE_ID, "options": {}}).status_code == 404
            assert bob.get(f"/files/{FILE_ID}/preview").status_code == 404
            assert alice.get("/files/missing-upload-id-1234/preview").status_code == 404

            assert claim_main([*command_prefix, "list"]) == 0
            assert FILE_ID in capsys.readouterr().out
            assert claim_main([*command_prefix, "claim", FILE_ID, "alice"]) == 0
            assert "Dry run only" in capsys.readouterr().out
            assert storage.get_record(FILE_ID).owner_user_id is None

            original_bytes = storage.file_path(FILE_ID).read_bytes()
            assert claim_main([*command_prefix, "claim", FILE_ID, "alice", "--apply"]) == 0
            assert storage.get_record(FILE_ID).owner_user_id == alice_id
            assert storage.file_path(FILE_ID).read_bytes() == original_bytes
            assert bob.post("/print", json={"file_id": FILE_ID, "options": {}}).status_code == 404

            # The chosen account passes the ownership check. Text has no preview.
            assert alice.get(f"/files/{FILE_ID}/preview").status_code == 400
            assert claim_main([*command_prefix, "list"]) == 0
            assert "0 unowned upload(s)" in capsys.readouterr().out
    finally:
        app.dependency_overrides.clear()


def test_claim_rejects_ownership_transfer_and_missing_data(tmp_path: Path) -> None:
    storage = TempFileStorage(tmp_path)
    create_legacy_upload(storage)
    storage.claim_legacy_file(FILE_ID, 1)

    with pytest.raises(LegacyClaimError, match="already has an owner"):
        storage.claim_legacy_file(FILE_ID, 2)
    assert storage.get_record(FILE_ID).owner_user_id == 1

    storage.file_path(FILE_ID).unlink()
    with pytest.raises(LegacyClaimError, match="not found"):
        storage.claim_legacy_file(FILE_ID, 2)
    assert storage.list_unowned_records() == []


def test_legacy_claim_keeps_upload_during_cleanup(tmp_path: Path, monkeypatch) -> None:
    storage = TempFileStorage(tmp_path)
    create_legacy_upload(storage)
    old = time.time() - 100 * 86400
    os.utime(storage.metadata_path(FILE_ID), (old, old))
    original_get_record = storage.get_record

    def cleanup_during_claim(file_id: str):
        storage.prune_expired(time.time() - 7 * 86400, set())
        return original_get_record(file_id)

    monkeypatch.setattr(storage, "get_record", cleanup_during_claim)
    claimed = storage.claim_legacy_file(FILE_ID, 1)

    assert claimed.owner_user_id == 1
    assert storage.file_path(FILE_ID).exists()
    assert original_get_record(FILE_ID).owner_user_id == 1


def test_claim_rejects_unknown_account_without_creating_database(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    storage = TempFileStorage(tmp_path / "uploads")
    create_legacy_upload(storage)
    absent_db = tmp_path / "absent.db"
    command_prefix = ["--tmp-dir", str(storage.root), "--db-path", str(absent_db)]

    with pytest.raises(SystemExit) as exc:
        claim_main([*command_prefix, "claim", FILE_ID, "alice", "--apply"])
    assert exc.value.code == 2
    assert not absent_db.exists()

    database = Database(absent_db)
    with pytest.raises(SystemExit) as exc:
        claim_main([*command_prefix, "claim", FILE_ID, "alice", "--apply"])
    assert exc.value.code == 2
    assert "User does not exist" in capsys.readouterr().err
    assert storage.get_record(FILE_ID).owner_user_id is None
