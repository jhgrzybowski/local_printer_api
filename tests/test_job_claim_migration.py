from __future__ import annotations

import sqlite3
from pathlib import Path

from app.services.database import Database


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
