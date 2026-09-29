from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.main import app, get_cups_client
from app.services.cups_client import CupsClientError, normalize_job
from app.services.database import Database, JobClaim
from tests.helpers import signup_user
from tests.test_print_api import FakeCupsClient


def history_row(database: Database, user_id: int, job_id: int) -> int:
    return database.insert_print_history(
        user_id=user_id,
        file_id=f"file-{job_id}",
        original_filename=f"job-{job_id}.pdf",
        detected_mime="application/pdf",
        size_bytes=100,
        page_count=1,
        requested_options={},
        applied_options={},
        cups_job_id=job_id,
        warnings=[],
        job_claim=JobClaim(
            user_id, job_id, "ipp://localhost/printers/Canon_MG5350",
            1000 + job_id, f"urn:uuid:job-{job_id}",
        ),
    )


def set_job_state(cups: FakeCupsClient, job_id: int, state_code: int) -> None:
    cups.jobs[job_id] = normalize_job(job_id, {
        "job-name": f"job-{job_id}.pdf",
        "job-state": state_code,
        "job-printer-uri": "ipp://localhost/printers/Canon_MG5350",
        "time-at-creation": 1000 + job_id,
        "job-uuid": f"urn:uuid:job-{job_id}",
    })


def test_history_follows_cups_completion_and_abort(isolated_database: Database) -> None:
    cups = FakeCupsClient()
    set_job_state(cups, 456, 5)
    app.dependency_overrides[get_cups_client] = lambda: cups
    try:
        with TestClient(app) as client:
            user_id = signup_user(client)["user"]["id"]
            completed_id = history_row(isolated_database, user_id, 123)
            aborted_id = history_row(isolated_database, user_id, 456)
            first = client.get(f"/history/{completed_id}").json()
            assert first["status"] == "processing"
            assert first["updated_at"] > first["created_at"]

            set_job_state(cups, 123, 9)
            set_job_state(cups, 456, 8)
            by_id = {row["id"]: row for row in client.get("/history").json()["history"]}
            assert by_id[completed_id]["status"] == "completed"
            assert by_id[completed_id]["updated_at"] > first["updated_at"]
            assert by_id[aborted_id]["status"] == "aborted"
    finally:
        app.dependency_overrides.clear()


def test_history_records_cancel_request_and_successful_forget(isolated_database: Database) -> None:
    cups = FakeCupsClient()
    app.dependency_overrides[get_cups_client] = lambda: cups
    try:
        with TestClient(app) as client:
            user_id = signup_user(client)["user"]["id"]
            history_id = history_row(isolated_database, user_id, 123)
            assert client.delete("/jobs/123").json()["cancelled"] is True
            requested = client.get(f"/history/{history_id}").json()
            assert requested["status"] == "cancel-requested"
            assert requested["updated_at"] > requested["created_at"]

            set_job_state(cups, 123, 7)
            canceled = client.get(f"/history/{history_id}").json()
            assert canceled["status"] == "canceled"
            assert canceled["updated_at"] > requested["updated_at"]

            assert client.post("/jobs/123/forget").json()["forgotten"] is True
            forgotten = client.get(f"/history/{history_id}").json()
            assert forgotten["status"] == "forgotten"
            assert forgotten["updated_at"] > canceled["updated_at"]
    finally:
        app.dependency_overrides.clear()


def test_reused_job_id_does_not_change_old_history(isolated_database: Database) -> None:
    cups = FakeCupsClient()
    app.dependency_overrides[get_cups_client] = lambda: cups
    try:
        with TestClient(app) as client:
            user_id = signup_user(client)["user"]["id"]
            history_id = history_row(isolated_database, user_id, 123)
            cups.jobs[123]["created_at"] = 999999
            cups.jobs[123]["state"] = "completed"
            assert client.get(f"/history/{history_id}").json()["status"] == "submitted"
    finally:
        app.dependency_overrides.clear()


def test_cups_outage_keeps_last_known_history(isolated_database: Database) -> None:
    cups = FakeCupsClient()
    app.dependency_overrides[get_cups_client] = lambda: cups
    try:
        with TestClient(app) as client:
            user_id = signup_user(client)["user"]["id"]
            history_id = history_row(isolated_database, user_id, 123)
            assert client.get(f"/history/{history_id}").json()["status"] == "processing"

            def unavailable(scope: str = "all", first_job_id: int | None = None) -> list[dict[str, object]]:
                raise CupsClientError("CUPS unavailable")

            cups.list_jobs = unavailable  # type: ignore[method-assign]
            response = client.get(f"/history/{history_id}")
            assert response.status_code == 200
            assert response.json()["status"] == "processing"
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize(
    ("method", "path", "job_id", "result_key"),
    [
        ("delete", "/jobs/123", 123, "cancelled"),
        ("post", "/jobs/456/forget", 456, "forgotten"),
    ],
)
def test_history_write_failure_does_not_hide_successful_cups_action(
    isolated_database: Database,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    method: str,
    path: str,
    job_id: int,
    result_key: str,
) -> None:
    cups = FakeCupsClient()
    app.dependency_overrides[get_cups_client] = lambda: cups
    try:
        with TestClient(app) as client:
            user_id = signup_user(client)["user"]["id"]
            history_id = history_row(isolated_database, user_id, job_id)

            def fail_update(*args: object, **kwargs: object) -> None:
                raise RuntimeError("history write failed")

            monkeypatch.setattr(isolated_database, "update_print_history_status_for_claim", fail_update)
            response = getattr(client, method)(path)

            assert response.status_code == 200
            assert response.json()[result_key] is True
            assert isolated_database.get_print_history(user_id, history_id)["status"] == "submitted"
            assert "Failed to persist print history status" in caplog.text
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize("path", ["/history", "/history/{history_id}"])
def test_refresh_write_failure_returns_last_known_history(
    isolated_database: Database,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    path: str,
) -> None:
    cups = FakeCupsClient()
    app.dependency_overrides[get_cups_client] = lambda: cups
    try:
        with TestClient(app) as client:
            user_id = signup_user(client)["user"]["id"]
            history_id = history_row(isolated_database, user_id, 123)

            def fail_update(*args: object, **kwargs: object) -> None:
                raise RuntimeError("history is read-only")

            monkeypatch.setattr(isolated_database, "update_print_history_status_for_claim", fail_update)
            response = client.get(path.format(history_id=history_id))

            assert response.status_code == 200
            entry = response.json()["history"][0] if path == "/history" else response.json()
            assert entry["status"] == "submitted"
            assert "Failed to persist refreshed print history status" in caplog.text
    finally:
        app.dependency_overrides.clear()


def test_successful_purge_recovers_after_transient_history_write_failure(
    isolated_database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cups = FakeCupsClient()
    app.dependency_overrides[get_cups_client] = lambda: cups
    try:
        with TestClient(app) as client:
            user_id = signup_user(client)["user"]["id"]
            history_id = history_row(isolated_database, user_id, 456)
            claim = isolated_database.list_job_claims(user_id)[0]

            def fail_update(*args: object, **kwargs: object) -> None:
                raise RuntimeError("SQLite is locked")

            monkeypatch.setattr(isolated_database, "update_print_history_status_for_claim", fail_update)
            assert client.post("/jobs/456/forget").json()["forgotten"] is True
            marker = isolated_database.forgotten_job_marker_path(claim)
            assert marker.exists()
            assert isolated_database.get_print_history(user_id, history_id)["status"] == "submitted"

            cups.jobs.pop(456)  # CUPS no longer has the purged job.
            app.state.database = Database(isolated_database.path)  # Recovery survives process restart.
            assert client.get(f"/history/{history_id}").json()["status"] == "forgotten"
            assert not marker.exists()
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize(
    ("terminal_state", "expected_status"),
    [(9, "completed"), (None, "submitted")],
)
def test_unsuccessful_cancel_uses_fresh_cups_state(
    isolated_database: Database,
    terminal_state: int | None,
    expected_status: str,
) -> None:
    class ChangingCups(FakeCupsClient):
        def cancel_job(
            self, job_id: int, expected_job: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            if terminal_state is None:
                self.jobs.pop(job_id)
            else:
                set_job_state(self, job_id, terminal_state)
            return super().cancel_job(job_id, expected_job=expected_job)

    cups = ChangingCups()
    app.dependency_overrides[get_cups_client] = lambda: cups
    try:
        with TestClient(app) as client:
            user_id = signup_user(client)["user"]["id"]
            history_id = history_row(isolated_database, user_id, 123)

            response = client.delete("/jobs/123")

            assert response.status_code == 200
            assert response.json()["cancelled"] is False
            assert isolated_database.get_print_history(user_id, history_id)["status"] == expected_status
    finally:
        app.dependency_overrides.clear()


def test_purge_marker_does_not_apply_to_replaced_database(tmp_path: Path) -> None:
    path = tmp_path / "app.db"
    old_database = Database(path)
    old_user = old_database.create_user("old", None, "hash", "salt", 1)
    history_row(old_database, old_user.id, 123)
    claim = old_database.list_job_claims(old_user.id)[0]
    old_database.save_forgotten_job_marker(claim)
    marker = old_database.forgotten_job_marker_path(claim)
    old_database_id = old_database.database_id()

    path.unlink()
    new_database = Database(path)
    new_user = new_database.create_user("new", None, "hash", "salt", 1)
    new_history_id = history_row(new_database, new_user.id, 123)
    new_database.recover_forgotten_job_markers(new_user.id)

    assert old_database_id != new_database.database_id()
    assert marker.exists()
    assert new_database.get_print_history(new_user.id, new_history_id)["status"] == "submitted"
