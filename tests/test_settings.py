from __future__ import annotations

import runpy
from pathlib import Path

import app.settings


def test_database_default_is_outside_temporary_storage(monkeypatch) -> None:
    monkeypatch.delenv("DB_PATH", raising=False)
    settings = runpy.run_path(app.settings.__file__)

    assert settings["DB_PATH"] == "/var/lib/local-printer-api/app.db"
    assert not Path(settings["DB_PATH"]).is_relative_to(Path(settings["TMP_DIR"]))


def test_database_path_can_be_overridden(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "printer.db"
    monkeypatch.setenv("DB_PATH", str(db_path))

    assert runpy.run_path(app.settings.__file__)["DB_PATH"] == str(db_path)
