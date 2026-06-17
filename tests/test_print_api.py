from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfWriter

from app.main import app, get_cups_client, get_file_storage
from app.services.cups_client import CupsClientError, normalize_job
from app.services.database import Database
from app.services.file_storage import TempFileStorage
from tests.helpers import signup_user


@pytest.fixture
def storage(tmp_path: Path) -> TempFileStorage:
    return TempFileStorage(tmp_path, max_upload_mb=1)


@pytest.fixture
def client(storage: TempFileStorage, isolated_database: Database) -> TestClient:
    app.dependency_overrides.clear()
    app.dependency_overrides[get_file_storage] = lambda: storage
    app.dependency_overrides[get_cups_client] = lambda: FakeCupsClient()
    with TestClient(app) as test_client:
        user = signup_user(test_client)
        for job_id in (123, 456, 789):
            grant_cups_job(isolated_database, job_id, user["user"]["id"])
        yield test_client
    app.dependency_overrides.clear()


class FakeCupsClient:
    queue_name = "Canon_MG5350"

    def __init__(self, queue: dict[str, Any] | None = None) -> None:
        self.queue = queue or ready_queue()
        self.submissions: list[dict[str, Any]] = []
        self.jobs = {
            123: normalize_job(123, {"job-name": "active.pdf", "job-state": 5}),
            456: normalize_job(456, {"job-name": "done.pdf", "job-state": 9}),
            789: normalize_job(789, {"job-name": "canceled.pdf", "job-state": 7}),
        }

    def get_queue(self) -> dict[str, Any]:
        return self.queue

    def get_option_capabilities(self) -> dict[str, set[str]]:
        return {
            "PageSize": {"A4"},
            "ColorModel": {"Gray", "RGB"},
            "Duplex": {"None", "DuplexNoTumble", "DuplexTumble"},
            "Quality": {"Normal", "High"},
        }

    def print_file(self, path: Path, title: str, options: dict[str, str]) -> int:
        self.submissions.append({"path": path, "title": title, "options": options})
        return 123

    def list_jobs(self, scope: str = "active") -> list[dict[str, Any]]:
        if scope == "active":
            return [job for job in self.jobs.values() if job["is_active"]]
        if scope == "completed":
            return [job for job in self.jobs.values() if job["is_terminal"]]
        return list(self.jobs.values())

    def job_counts(self) -> dict[str, int]:
        return {
            "active": len(self.list_jobs("active")),
            "completed": len(self.list_jobs("completed")),
            "all": len(self.list_jobs("all")),
        }

    def get_job(self, job_id: int) -> dict[str, Any] | None:
        return self.jobs.get(job_id)

    def cancel_job(self, job_id: int) -> dict[str, Any]:
        job = self.get_job(job_id)
        if job is None:
            return {
                "job_id": job_id,
                "cancelled": False,
                "already_terminal": False,
                "can_forget": False,
                "message": "Job was not found.",
            }
        if not job["can_cancel"]:
            return {
                "job_id": job_id,
                "cancelled": False,
                "already_terminal": job["is_terminal"],
                "can_forget": job["can_forget"],
                "message": "Job is already completed/canceled/aborted and cannot be cancelled.",
            }
        return {
            "job_id": job_id,
            "cancelled": True,
            "already_terminal": False,
            "can_forget": False,
            "message": "Job cancellation was submitted.",
        }

    def forget_job(self, job_id: int) -> dict[str, Any]:
        job = self.get_job(job_id)
        if job is None:
            return {
                "job_id": job_id,
                "forgotten": False,
                "method": "pycups-purge-job",
                "reason": "Job was not found.",
            }
        if job["can_cancel"]:
            return {
                "job_id": job_id,
                "forgotten": False,
                "method": "pycups-purge-job",
                "reason": "Job is still active; cancel it before purging history.",
            }
        return {"job_id": job_id, "forgotten": True, "method": "pycups-purge-job"}


class FailingCupsClient(FakeCupsClient):
    def get_queue(self) -> dict[str, Any]:
        raise CupsClientError("CUPS unavailable")


def ready_queue() -> dict[str, Any]:
    return {
        "name": "Canon_MG5350",
        "exists": True,
        "attributes": {
            "printer-state": 3,
            "printer-is-accepting-jobs": True,
            "printer-state-reasons": ["none"],
        },
    }


def missing_queue() -> dict[str, Any]:
    return {"name": "Canon_MG5350", "exists": False, "attributes": {}}


def stopped_queue() -> dict[str, Any]:
    queue = ready_queue()
    queue["attributes"]["printer-state"] = 5
    return queue


def make_pdf(page_count: int = 2) -> bytes:
    buffer = BytesIO()
    writer = PdfWriter()
    for _ in range(page_count):
        writer.add_blank_page(width=72, height=72)
    writer.write(buffer)
    return buffer.getvalue()


def make_png() -> bytes:
    from PIL import Image

    buffer = BytesIO()
    image = Image.new("RGB", (4, 4), color="white")
    image.save(buffer, "PNG")
    return buffer.getvalue()


def upload_pdf(client: TestClient, page_count: int = 2) -> str:
    response = client.post(
        "/files",
        files={"file": ("print.pdf", make_pdf(page_count), "application/pdf")},
    )
    assert response.status_code == 200
    return response.json()["file_id"]


def upload_png(client: TestClient) -> str:
    response = client.post(
        "/files",
        files={"file": ("image.png", make_png(), "image/png")},
    )
    assert response.status_code == 200
    return response.json()["file_id"]


def grant_cups_job(database: Database, job_id: int, user_id: int = 1) -> None:
    database.insert_print_history(
        user_id=user_id,
        file_id=f"test-file-{job_id}",
        original_filename=f"job-{job_id}.pdf",
        detected_mime="application/pdf",
        size_bytes=123,
        page_count=1,
        requested_options={},
        applied_options={},
        cups_job_id=job_id,
        warnings=[],
    )


def test_print_pdf_with_mocked_cups(client: TestClient) -> None:
    file_id = upload_pdf(client, 3)

    response = client.post(
        "/print",
        json={
            "file_id": file_id,
            "options": {
                "copies": 2,
                "pages": "1,3",
                "paper_size": "A4",
                "color_mode": "monochrome",
                "duplex": "none",
            },
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["job_id"] == 123
    assert body["queue"] == "Canon_MG5350"
    assert body["applied_options"]["copies"] == "2"
    assert body["applied_options"]["PageSize"] == "A4"
    assert body["unsupported_options"] == []
    assert any("page order" in warning for warning in body["warnings"])


def test_print_missing_file_id(client: TestClient) -> None:
    response = client.post("/print", json={"file_id": "missing", "options": {}})

    assert response.status_code == 404


def test_print_invalid_page_range(client: TestClient) -> None:
    file_id = upload_pdf(client, 3)

    response = client.post("/print", json={"file_id": file_id, "options": {"pages": "3-1"}})

    assert response.status_code == 400
    assert "Reversed page range" in response.json()["detail"]


def test_print_rejects_image_page_range_other_than_one(client: TestClient) -> None:
    file_id = upload_png(client)

    response = client.post("/print", json={"file_id": file_id, "options": {"pages": "2"}})

    assert response.status_code == 400
    assert "outside document bounds" in response.json()["detail"]


def test_print_reports_cups_unavailable(storage: TempFileStorage) -> None:
    app.dependency_overrides.clear()
    app.dependency_overrides[get_file_storage] = lambda: storage
    app.dependency_overrides[get_cups_client] = lambda: FailingCupsClient()
    with TestClient(app) as client:
        signup_user(client)
        file_id = upload_pdf(client)
        response = client.post("/print", json={"file_id": file_id, "options": {}})

    app.dependency_overrides.clear()
    assert response.status_code == 503
    assert response.json()["detail"] == "CUPS unavailable"


def test_print_reports_queue_missing(storage: TempFileStorage) -> None:
    app.dependency_overrides.clear()
    app.dependency_overrides[get_file_storage] = lambda: storage
    app.dependency_overrides[get_cups_client] = lambda: FakeCupsClient(missing_queue())
    with TestClient(app) as client:
        signup_user(client)
        file_id = upload_pdf(client)
        response = client.post("/print", json={"file_id": file_id, "options": {}})

    app.dependency_overrides.clear()
    assert response.status_code == 503
    assert "missing" in response.json()["detail"]


def test_print_reports_queue_stopped(storage: TempFileStorage) -> None:
    app.dependency_overrides.clear()
    app.dependency_overrides[get_file_storage] = lambda: storage
    app.dependency_overrides[get_cups_client] = lambda: FakeCupsClient(stopped_queue())
    with TestClient(app) as client:
        signup_user(client)
        file_id = upload_pdf(client)
        response = client.post("/print", json={"file_id": file_id, "options": {}})

    app.dependency_overrides.clear()
    assert response.status_code == 409


def test_jobs_defaults_to_active_scope(client: TestClient) -> None:
    list_response = client.get("/jobs")

    assert list_response.status_code == 200
    body = list_response.json()
    assert body["scope"] == "active"
    assert body["queue"] == "Canon_MG5350"
    assert [job["job_id"] for job in body["jobs"]] == [123]
    assert body["counts"] == {"active": 1, "completed": 2, "all": 3}
    assert body["jobs"][0]["can_cancel"] is True
    assert body["jobs"][0]["is_active"] is True


def test_jobs_all_scope_includes_history(client: TestClient) -> None:
    response = client.get("/jobs?scope=all")

    assert response.status_code == 200
    body = response.json()
    assert body["scope"] == "all"
    assert [job["job_id"] for job in body["jobs"]] == [123, 456, 789]
    assert body["jobs"][1]["can_cancel"] is False
    assert body["jobs"][1]["can_forget"] is True


def test_jobs_rejects_invalid_scope(client: TestClient) -> None:
    response = client.get("/jobs?scope=old")

    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid job scope"


def test_get_job_returns_lifecycle_flags(client: TestClient) -> None:
    get_response = client.get("/jobs/123")

    assert get_response.status_code == 200
    assert get_response.json()["state"] == "processing"
    assert get_response.json()["can_cancel"] is True


def test_cancel_active_job(client: TestClient, isolated_database: Database) -> None:
    grant_cups_job(isolated_database, 123)

    cancel_response = client.delete("/jobs/123")

    assert cancel_response.status_code == 200
    assert cancel_response.json()["job_id"] == 123
    assert cancel_response.json()["cancelled"] is True


def test_cancel_terminal_job_returns_domain_response(
    client: TestClient,
    isolated_database: Database,
) -> None:
    grant_cups_job(isolated_database, 456)

    response = client.delete("/jobs/456")

    assert response.status_code == 200
    assert response.json() == {
        "job_id": 456,
        "cancelled": False,
        "already_terminal": True,
        "can_forget": True,
        "message": "Job is already completed/canceled/aborted and cannot be cancelled.",
    }


def test_cancel_missing_job_returns_not_found_without_user_history(client: TestClient) -> None:
    missing_cancel_response = client.delete("/jobs/999")

    assert missing_cancel_response.status_code == 404


def test_forget_terminal_job(client: TestClient, isolated_database: Database) -> None:
    grant_cups_job(isolated_database, 456)

    response = client.post("/jobs/456/forget")

    assert response.status_code == 200
    assert response.json() == {
        "job_id": 456,
        "forgotten": True,
        "method": "pycups-purge-job",
    }


def test_forget_active_job_returns_conflict(
    client: TestClient,
    isolated_database: Database,
) -> None:
    grant_cups_job(isolated_database, 123)

    response = client.post("/jobs/123/forget")

    assert response.status_code == 409
    assert response.json()["detail"]["forgotten"] is False
    assert "still active" in response.json()["detail"]["reason"]


def test_options_endpoint_returns_detected_capabilities(client: TestClient) -> None:
    response = client.get("/options")

    assert response.status_code == 200
    assert response.json()["queue"] == "Canon_MG5350"
    assert response.json()["paper_sizes"]["choices"] == ["A4"]
    assert response.json()["duplex_modes"]["mapping"]["none"] == "None"
