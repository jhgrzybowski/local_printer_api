from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest
from app.main import app
from app.services.cups_client import clear_profile_cache
from app.services.database import Database
from app.services.printer_reachability import clear_probe_cache


@pytest.fixture(autouse=True)
def isolated_database(tmp_path: Path) -> Iterator[Database]:
    database = Database(tmp_path / "app.db")
    app.state.database = database
    yield database
    if hasattr(app.state, "database"):
        delattr(app.state, "database")


@pytest.fixture(autouse=True)
def isolated_caches() -> Iterator[None]:
    """Printer probes and capabilities are cached per process; reset them."""
    clear_probe_cache()
    clear_profile_cache()
    yield
    clear_probe_cache()
    clear_profile_cache()
