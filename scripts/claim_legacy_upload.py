#!/usr/bin/env python3
"""Inventory and explicitly assign uploads created before user accounts existed."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.auth import AuthError, normalize_username
from app.services.file_storage import LegacyClaimError, TempFileStorage
from app.settings import DB_PATH, TMP_DIR


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tmp-dir", type=Path, default=Path(TMP_DIR))
    parser.add_argument("--db-path", type=Path, default=Path(DB_PATH))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="List readable uploads with no owner")
    claim = commands.add_parser("claim", help="Assign exactly one upload to an existing user")
    claim.add_argument("file_id")
    claim.add_argument("username")
    claim.add_argument("--apply", action="store_true", help="Write the ownership change")
    args = parser.parse_args(argv)

    storage = TempFileStorage(args.tmp_dir)
    if args.command == "list":
        if not storage.metadata_dir.is_dir():
            parser.error(f"Metadata directory does not exist: {storage.metadata_dir}")
        records = storage.list_unowned_records()
        for record in records:
            print(f"{record.file_id}\t{record.original_filename}\t{record.size_bytes} bytes")
        print(f"{len(records)} unowned upload(s)")
        return 0

    if not args.db_path.is_file():
        parser.error(f"Account database does not exist: {args.db_path}")
    try:
        username = normalize_username(args.username)
    except AuthError as exc:
        parser.error(exc.message)
    try:
        # Open read-only: a typo must not create or migrate a different database.
        with closing(sqlite3.connect(f"{args.db_path.resolve().as_uri()}?mode=ro", uri=True)) as db:
            row = db.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
    except sqlite3.Error as exc:
        parser.error(f"Cannot read account database: {exc}")
    if row is None:
        parser.error(f"User does not exist: {username}")
    user_id = int(row[0])

    record = storage.get_record(args.file_id)
    if record is None or record.file_id != args.file_id or not storage.file_path(args.file_id).is_file():
        parser.error("Upload or metadata not found")
    if record.owner_user_id is not None:
        parser.error("Upload already has an owner")

    print(f"Upload: {record.file_id} ({record.original_filename}, {record.size_bytes} bytes)")
    print(f"Target owner: {username} (user ID {user_id})")
    if not args.apply:
        print("Dry run only. Add --apply to claim this upload.")
        return 0
    try:
        storage.claim_legacy_file(record.file_id, user_id)
    except (LegacyClaimError, OSError) as exc:
        parser.error(str(exc))
    print("Claimed. The selected account can now preview and print this upload.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
