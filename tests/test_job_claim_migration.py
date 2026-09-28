from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import sqlite3
from pathlib import Path
from threading import Barrier

from app.services.database import Database, JobClaim


def test_existing_history_schema_gains_nullable_identity_columns(tmp_path: Path) -> None:
    path = tmp_path / "existing.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE print_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                file_id TEXT NOT NULL,
                original_filename TEXT NOT NULL,
                detected_mime TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                page_count INTEGER,
                requested_options_json TEXT NOT NULL,
                applied_options_json TEXT NOT NULL,
                cups_job_id INTEGER NOT NULL,
                warnings_json TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            INSERT INTO print_history VALUES (
                1, 1, 'old-file', 'old.pdf', 'application/pdf', 1, 1,
                '{}', '{}', 123, '[]', 'submitted', '2026-01-01', '2026-01-01'
            );
            """
        )

    database = Database(path)
    database.migrate()
    with database.connect() as connection:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(print_history)")}
        old_row = connection.execute("SELECT * FROM print_history WHERE id = 1").fetchone()

    assert {"cups_printer_uri", "cups_created_at", "cups_job_uuid"} <= columns
    assert old_row is not None
    assert old_row["cups_printer_uri"] is None
    assert database.list_job_claims(1) == []


def test_fallback_claims_are_bound_to_database_identity(tmp_path: Path) -> None:
    path = tmp_path / "app.db"
    original = Database(path)
    original_id = original.database_id()
    claim = JobClaim(1, 321, "ipp://localhost/printers/Canon_MG5350", 1321, "job-uuid")
    original.save_fallback_job_claim(claim)
    assert Database(path).list_fallback_job_claims(1) == [claim]

    # A different DB_PATH in the same directory must not inherit the claim.
    assert Database(tmp_path / "other.db").list_fallback_job_claims(1) == []

    # Recreating the original path also starts a fresh identity and user IDs.
    path.unlink()
    replacement = Database(path)
    assert replacement.database_id() != original_id
    assert replacement.list_fallback_job_claims(1) == []


def test_concurrent_startup_serializes_identity_column_migration(tmp_path: Path) -> None:
    path = tmp_path / "concurrent.db"
    start = Barrier(8)

    def initialize() -> str:
        start.wait(timeout=5)
        return Database(path).database_id()

    with ThreadPoolExecutor(max_workers=8) as pool:
        identities = list(pool.map(lambda _: initialize(), range(8)))

    assert len(set(identities)) == 1
    with Database(path).connect() as connection:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(print_history)")}
    assert {"cups_printer_uri", "cups_created_at", "cups_job_uuid"} <= columns
