from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from pypdf import PdfWriter

from app.main import app, get_cups_client, get_file_storage
from app.services.auth import hash_token
from app.services.database import Database
from app.services.file_storage import TempFileStorage
from app.settings import SESSION_COOKIE_NAME
from tests.helpers import signup_user


class FakeCupsClient:
    queue_name = "Canon_MG5350"

    def get_queue(self) -> dict[str, Any]:
        return {
            "name": "Canon_MG5350",
            "exists": True,
            "attributes": {
                "printer-state": 3,
                "printer-is-accepting-jobs": True,
                "printer-state-reasons": ["none"],
            },
        }

    def get_option_capabilities(self) -> dict[str, set[str]]:
        return {
            "PageSize": {"A4"},
            "ColorModel": {"Gray", "RGB"},
        }

    def print_file(self, path: Path, title: str, options: dict[str, str]) -> int:
        return 321


def make_pdf() -> bytes:
    buffer = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.write(buffer)
    return buffer.getvalue()


def test_signup_creates_session_and_me_returns_user() -> None:
    app.dependency_overrides.clear()
    with TestClient(app) as client:
        body = signup_user(client, username="Alice")
        response = client.get("/auth/me")

    app.dependency_overrides.clear()
    assert body["user"]["username"] == "alice"
    assert response.status_code == 200
    assert response.json()["user"]["username"] == "alice"


def test_duplicate_signup_rejected() -> None:
    app.dependency_overrides.clear()
    with TestClient(app) as client:
        signup_user(client)
        response = client.post(
            "/auth/signup",
            json={"username": "alice", "password": "correct horse"},
        )

    app.dependency_overrides.clear()
    assert response.status_code == 409
    assert response.json()["detail"] == "Username already exists"


def test_login_failure_success_and_logout() -> None:
    app.dependency_overrides.clear()
    with TestClient(app) as client:
        signup_user(client, password="right-password")
        client.post("/auth/logout")

        failure = client.post(
            "/auth/login",
            json={"username": "alice", "password": "wrong-password"},
        )
        success = client.post(
            "/auth/login",
            json={"username": "alice", "password": "right-password"},
        )
        logout = client.post("/auth/logout")
        me = client.get("/auth/me")

    app.dependency_overrides.clear()
    assert failure.status_code == 401
    assert success.status_code == 200
    assert logout.json() == {"logged_out": True}
    assert me.status_code == 401


def test_auth_required_endpoints_return_401_when_unauthenticated() -> None:
    app.dependency_overrides.clear()
    with TestClient(app) as client:
        responses = [
            client.get("/jobs"),
            client.post("/files", files={"file": ("x.pdf", make_pdf(), "application/pdf")}),
            client.post("/print", json={"file_id": "missing", "options": {}}),
        ]

    app.dependency_overrides.clear()
    assert [response.status_code for response in responses] == [401, 401, 401]


def test_invalid_and_expired_sessions_return_401(isolated_database: Database) -> None:
    app.dependency_overrides.clear()
    with TestClient(app) as client:
        signup_user(client)
        token = client.cookies.get(SESSION_COOKIE_NAME)
        assert token is not None

        with isolated_database.connect() as connection:
            connection.execute(
                "UPDATE sessions SET expires_at = ? WHERE token_hash = ?",
                ("2000-01-01T00:00:00+00:00", hash_token(token)),
            )

        expired = client.get("/auth/me")
        client.cookies.set(SESSION_COOKIE_NAME, "not-a-real-session")
        invalid = client.get("/auth/me")

    app.dependency_overrides.clear()
    assert expired.status_code == 401
    assert invalid.status_code == 401


def test_preferences_are_persisted_and_user_scoped() -> None:
    app.dependency_overrides.clear()
    with TestClient(app) as alice, TestClient(app) as bob:
        signup_user(alice, username="alice")
        signup_user(bob, username="bob")

        alice_put = alice.put("/me/preferences", json={"paper_size": "A4", "copies": 2})
        bob_put = bob.put("/me/preferences", json={"paper_size": "Letter", "copies": 1})
        alice_get = alice.get("/me/preferences")
        bob_get = bob.get("/me/preferences")

    app.dependency_overrides.clear()
    assert alice_put.status_code == 200
    assert bob_put.status_code == 200
    assert alice_get.json()["preferences"] == {"paper_size": "A4", "copies": 2}
    assert bob_get.json()["preferences"] == {"paper_size": "Letter", "copies": 1}


def test_print_history_is_created_and_user_scoped(tmp_path: Path) -> None:
    storage = TempFileStorage(tmp_path / "files", max_upload_mb=1)
    app.dependency_overrides.clear()
    app.dependency_overrides[get_file_storage] = lambda: storage
    app.dependency_overrides[get_cups_client] = lambda: FakeCupsClient()

    with TestClient(app) as alice, TestClient(app) as bob:
        signup_user(alice, username="alice")
        signup_user(bob, username="bob")

        upload = alice.post(
            "/files",
            files={"file": ("history.pdf", make_pdf(), "application/pdf")},
        )
        assert upload.status_code == 200
        print_response = alice.post(
            "/print",
            json={"file_id": upload.json()["file_id"], "options": {"paper_size": "A4"}},
        )
        alice_history = alice.get("/history")
        bob_history = bob.get("/history")
        alice_entry = alice.get(f"/history/{print_response.json()['history_id']}")
        bob_entry = bob.get(f"/history/{print_response.json()['history_id']}")

    app.dependency_overrides.clear()
    assert print_response.status_code == 200
    assert print_response.json()["job_id"] == 321
    assert print_response.json()["history_id"] == alice_history.json()["history"][0]["id"]
    assert alice_entry.status_code == 200
    assert alice_entry.json()["original_filename"] == "history.pdf"
    assert bob_history.json()["history"] == []
    assert bob_entry.status_code == 404


def test_uploaded_files_are_user_scoped_for_preview_and_print(tmp_path: Path) -> None:
    storage = TempFileStorage(tmp_path / "files", max_upload_mb=1)
    app.dependency_overrides.clear()
    app.dependency_overrides[get_file_storage] = lambda: storage
    app.dependency_overrides[get_cups_client] = lambda: FakeCupsClient()

    with TestClient(app) as alice, TestClient(app) as bob:
        alice_user = signup_user(alice, username="alice")["user"]
        signup_user(bob, username="bob")

        upload = alice.post(
            "/files",
            files={"file": ("owned.pdf", make_pdf(), "application/pdf")},
        )
        assert upload.status_code == 200
        file_id = upload.json()["file_id"]

        bob_preview = bob.get(f"/files/{file_id}/preview")
        bob_print = bob.post("/print", json={"file_id": file_id, "options": {}})
        alice_print = alice.post("/print", json={"file_id": file_id, "options": {}})

    app.dependency_overrides.clear()
    stored_record = storage.get_record(file_id)
    assert stored_record is not None
    assert stored_record.owner_user_id == alice_user["id"]
    assert bob_preview.status_code == 404
    assert bob_print.status_code == 404
    assert alice_print.status_code == 200
