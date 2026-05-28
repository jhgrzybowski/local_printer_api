from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from app.settings import DB_PATH


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass(frozen=True)
class User:
    id: int
    username: str
    display_name: str | None
    created_at: str
    updated_at: str


class Database:
    def __init__(self, path: str | Path = DB_PATH) -> None:
        self.path = Path(path)
        self.migrate()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def migrate(self) -> None:
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE,
                    display_name TEXT,
                    password_hash TEXT NOT NULL,
                    password_salt TEXT NOT NULL,
                    password_iterations INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    token_hash TEXT NOT NULL UNIQUE,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    expires_at TEXT NOT NULL,
                    user_agent TEXT,
                    ip_address TEXT,
                    created_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_sessions_user_id ON sessions(user_id);
                CREATE INDEX IF NOT EXISTS idx_sessions_expires_at ON sessions(expires_at);

                CREATE TABLE IF NOT EXISTS user_preferences (
                    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                    preferences_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS print_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
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

                CREATE INDEX IF NOT EXISTS idx_print_history_user_created
                    ON print_history(user_id, created_at DESC);
                """
            )

    def create_user(
        self,
        username: str,
        display_name: str | None,
        password_hash: str,
        password_salt: str,
        password_iterations: int,
    ) -> User:
        now = utc_now()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO users (
                    username, display_name, password_hash, password_salt,
                    password_iterations, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    username,
                    display_name,
                    password_hash,
                    password_salt,
                    password_iterations,
                    now,
                    now,
                ),
            )
            user_id = int(cursor.lastrowid)
        user = self.get_user_by_id(user_id)
        if user is None:
            raise RuntimeError("Created user could not be loaded")
        return user

    def get_user_credentials(self, username: str) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute(
                """
                SELECT id, username, display_name, password_hash, password_salt,
                       password_iterations, created_at, updated_at
                FROM users
                WHERE username = ?
                """,
                (username,),
            ).fetchone()

    def get_user_by_id(self, user_id: int) -> User | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT id, username, display_name, created_at, updated_at
                FROM users
                WHERE id = ?
                """,
                (user_id,),
            ).fetchone()
        return user_from_row(row) if row is not None else None

    def create_session(
        self,
        user_id: int,
        token_hash: str,
        expires_at: str,
        user_agent: str | None,
        ip_address: str | None,
    ) -> None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO sessions (
                    token_hash, user_id, expires_at, user_agent, ip_address,
                    created_at, last_seen_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (token_hash, user_id, expires_at, user_agent, ip_address, now, now),
            )

    def get_user_for_session(self, token_hash: str) -> User | None:
        now = utc_now()
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT users.id, users.username, users.display_name,
                       users.created_at, users.updated_at
                FROM sessions
                JOIN users ON users.id = sessions.user_id
                WHERE sessions.token_hash = ?
                  AND sessions.expires_at > ?
                """,
                (token_hash, now),
            ).fetchone()
            if row is not None:
                connection.execute(
                    "UPDATE sessions SET last_seen_at = ? WHERE token_hash = ?",
                    (now, token_hash),
                )
        return user_from_row(row) if row is not None else None

    def delete_session(self, token_hash: str) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))

    def delete_expired_sessions(self) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM sessions WHERE expires_at <= ?", (utc_now(),))

    def get_preferences(self, user_id: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT preferences_json FROM user_preferences WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if row is None:
            return None
        return json.loads(str(row["preferences_json"]))

    def upsert_preferences(self, user_id: int, preferences: dict[str, Any]) -> dict[str, Any]:
        now = utc_now()
        encoded = json.dumps(preferences, sort_keys=True, separators=(",", ":"))
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO user_preferences (
                    user_id, preferences_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    preferences_json = excluded.preferences_json,
                    updated_at = excluded.updated_at
                """,
                (user_id, encoded, now, now),
            )
        return preferences

    def insert_print_history(
        self,
        user_id: int,
        file_id: str,
        original_filename: str,
        detected_mime: str,
        size_bytes: int,
        page_count: int | None,
        requested_options: dict[str, Any],
        applied_options: dict[str, Any],
        cups_job_id: int,
        warnings: list[str],
        status: str = "submitted",
    ) -> int:
        now = utc_now()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO print_history (
                    user_id, file_id, original_filename, detected_mime, size_bytes,
                    page_count, requested_options_json, applied_options_json,
                    cups_job_id, warnings_json, status, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    file_id,
                    original_filename,
                    detected_mime,
                    size_bytes,
                    page_count,
                    json.dumps(requested_options, sort_keys=True, separators=(",", ":")),
                    json.dumps(applied_options, sort_keys=True, separators=(",", ":")),
                    cups_job_id,
                    json.dumps(warnings, sort_keys=True, separators=(",", ":")),
                    status,
                    now,
                    now,
                ),
            )
            return int(cursor.lastrowid)

    def list_print_history(self, user_id: int) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM print_history
                WHERE user_id = ?
                ORDER BY created_at DESC, id DESC
                """,
                (user_id,),
            ).fetchall()
        return [history_from_row(row) for row in rows]

    def get_print_history(self, user_id: int, history_id: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM print_history
                WHERE user_id = ? AND id = ?
                """,
                (user_id, history_id),
            ).fetchone()
        return history_from_row(row) if row is not None else None

    def user_has_cups_job(self, user_id: int, cups_job_id: int) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT 1
                FROM print_history
                WHERE user_id = ? AND cups_job_id = ?
                LIMIT 1
                """,
                (user_id, cups_job_id),
            ).fetchone()
        return row is not None


def user_from_row(row: sqlite3.Row) -> User:
    return User(
        id=int(row["id"]),
        username=str(row["username"]),
        display_name=row["display_name"],
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def history_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "file_id": str(row["file_id"]),
        "original_filename": str(row["original_filename"]),
        "detected_mime": str(row["detected_mime"]),
        "size_bytes": int(row["size_bytes"]),
        "page_count": row["page_count"],
        "requested_options": json.loads(str(row["requested_options_json"])),
        "applied_options": json.loads(str(row["applied_options_json"])),
        "cups_job_id": int(row["cups_job_id"]),
        "warnings": json.loads(str(row["warnings_json"])),
        "status": str(row["status"]),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
    }
