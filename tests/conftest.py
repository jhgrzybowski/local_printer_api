from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest
from app.main import UNPERSISTED_JOB_OWNERS, app
from app.services.database import Database


@pytest.fixture(autouse=True)
def isolated_database(tmp_path: Path) -> Iterator[Database]:
    database = Database(tmp_path / "app.db")
    app.state.database = database
    UNPERSISTED_JOB_OWNERS.clear()
    yield database
    UNPERSISTED_JOB_OWNERS.clear()
    if hasattr(app.state, "database"):
        delattr(app.state, "database")
