from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfWriter

from app.main import app, get_file_storage
from app.services.file_storage import TempFileStorage, sanitize_filename
from app.services.preview import _save_png_atomic
from tests.helpers import signup_user


@pytest.fixture
def file_client(tmp_path: Path) -> TestClient:
    storage = TempFileStorage(tmp_path, max_upload_mb=1)
    app.dependency_overrides.clear()
    app.dependency_overrides[get_file_storage] = lambda: storage
    with TestClient(app) as client:
        signup_user(client)
        yield client
    app.dependency_overrides.clear()


def make_pdf(page_count: int = 1) -> bytes:
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


def test_upload_rejects_unsupported_mime(file_client: TestClient) -> None:
    response = file_client.post(
        "/files",
        files={"file": ("data.bin", b"\x00\x01\x02", "application/octet-stream")},
    )

    assert response.status_code == 415


def test_upload_rejects_empty_file(file_client: TestClient) -> None:
    response = file_client.post(
        "/files",
        files={"file": ("empty.pdf", b"", "application/pdf")},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Uploaded file is empty"


def test_pdf_page_count_works(file_client: TestClient) -> None:
    response = file_client.post(
        "/files",
        files={"file": ("two-pages.pdf", make_pdf(2), "application/pdf")},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["detected_mime"] == "application/pdf"
    assert body["page_count"] == 2
    assert body["preview_available"] is True


def test_corrupt_pdf_returns_400(file_client: TestClient) -> None:
    response = file_client.post(
        "/files",
        files={"file": ("broken.pdf", b"%PDF-1.7\nnot a valid pdf", "application/pdf")},
    )

    assert response.status_code == 400
    assert "PDF" in response.json()["detail"]


def test_preview_endpoint_returns_expected_metadata(file_client: TestClient) -> None:
    upload_response = file_client.post(
        "/files",
        files={"file": ("image.png", make_png(), "image/png")},
    )
    assert upload_response.status_code == 200
    file_id = upload_response.json()["file_id"]

    response = file_client.get(f"/files/{file_id}/preview")

    assert response.status_code == 200
    body = response.json()
    assert body["file_id"] == file_id
    assert body["page_count"] == 1
    assert body["pages"][0]["page"] == 1
    assert body["pages"][0]["url"] == f"/files/{file_id}/preview/1"
    page = file_client.get(f"/files/{file_id}/preview/1")
    assert page.status_code == 200
    assert page.content.startswith(b"\x89PNG")


def test_pdf_preview_list_allows_pages_not_rendered_yet(
    file_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.preview import PreviewService

    upload = file_client.post(
        "/files", files={"file": ("two-pages.pdf", make_pdf(2), "application/pdf")},
    )
    assert upload.status_code == 200
    file_id = upload.json()["file_id"]

    def pending_paths(service: PreviewService, record: object) -> list[Path]:
        preview_dir = service.storage.preview_dir(file_id)
        return [preview_dir / "page-1.png", preview_dir / "page-2.png"]

    monkeypatch.setattr(PreviewService, "ensure_previews", pending_paths)
    response = file_client.get(f"/files/{file_id}/preview")
    assert response.status_code == 200
    assert response.json()["pages"] == [
        {"page": 1, "url": f"/files/{file_id}/preview/1"},
        {"page": 2, "url": f"/files/{file_id}/preview/2"},
    ]


def test_pdf_preview_renders_only_requested_page(
    file_client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from PIL import Image
    import pdf2image

    rendered_pages: list[int] = []

    def render_page(source: str, **kwargs: object) -> list[Image.Image]:
        assert Path(source).exists()
        assert kwargs["first_page"] == kwargs["last_page"]
        assert kwargs["size"] == 1600
        assert kwargs["timeout"] == 30
        rendered_pages.append(int(kwargs["first_page"]))
        return [Image.new("RGB", (4, 4), color="white")]

    monkeypatch.setattr(pdf2image, "convert_from_path", render_page)
    upload_response = file_client.post(
        "/files",
        files={"file": ("three-pages.pdf", make_pdf(3), "application/pdf")},
    )
    assert upload_response.status_code == 200
    file_id = upload_response.json()["file_id"]
    preview_dir = tmp_path / "previews" / file_id

    metadata = file_client.get(f"/files/{file_id}/preview")
    assert metadata.status_code == 200
    assert [page["page"] for page in metadata.json()["pages"]] == [1, 2, 3]
    assert all("size_bytes" not in page for page in metadata.json()["pages"])
    assert rendered_pages == []
    assert not preview_dir.exists()

    page = file_client.get(f"/files/{file_id}/preview/2")
    assert page.status_code == 200
    assert page.headers["content-type"] == "image/png"
    assert rendered_pages == [2]
    assert sorted(path.name for path in preview_dir.iterdir()) == ["page-2.png"]

    assert file_client.get(f"/files/{file_id}/preview/2").status_code == 200
    assert rendered_pages == [2]
    metadata = file_client.get(f"/files/{file_id}/preview")
    assert metadata.json()["pages"][1]["size_bytes"] == len(page.content)
    assert "size_bytes" not in metadata.json()["pages"][0]


def test_unknown_file_id_returns_404(file_client: TestClient) -> None:
    response = file_client.get("/files/not-a-real-file/preview")

    assert response.status_code == 404


def test_invalid_preview_page_returns_400(file_client: TestClient) -> None:
    upload_response = file_client.post(
        "/files",
        files={"file": ("image.png", make_png(), "image/png")},
    )
    assert upload_response.status_code == 200
    file_id = upload_response.json()["file_id"]

    response = file_client.get(f"/files/{file_id}/preview/not-a-page")

    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid page number"


def test_storage_sanitizes_filenames() -> None:
    assert sanitize_filename("../../bad name.pdf") == "bad_name.pdf"
    assert sanitize_filename(r"..\..\nested\file.jpg") == "file.jpg"
    assert sanitize_filename("-" * 200 + ".docx") == "upload.docx"
    assert sanitize_filename("---.docx") == "upload.docx"


def test_image_upload_rejects_pixel_limit(
    file_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services import file_storage

    monkeypatch.setattr(file_storage, "MAX_IMAGE_PIXELS", 15)
    response = file_client.post(
        "/files",
        files={"file": ("large.png", make_png(), "image/png")},
    )

    assert response.status_code == 413
    assert "MAX_IMAGE_PIXELS" in response.json()["detail"]


def test_image_preview_rechecks_pixel_limit(
    file_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services import preview

    uploaded = file_client.post(
        "/files",
        files={"file": ("image.png", make_png(), "image/png")},
    )
    assert uploaded.status_code == 200
    monkeypatch.setattr(preview, "MAX_IMAGE_PIXELS", 15)

    response = file_client.get(f"/files/{uploaded.json()['file_id']}/preview/1")

    assert response.status_code == 413
    assert "MAX_IMAGE_PIXELS" in response.json()["detail"]


def test_preview_is_published_only_after_png_write_completes(tmp_path: Path) -> None:
    destination = tmp_path / "page-1.png"

    class SlowImage:
        def save(self, path: Path, image_format: str) -> None:
            assert image_format == "PNG"
            path.write_bytes(b"partial")
            assert not destination.exists()
            path.write_bytes(b"complete")

    _save_png_atomic(SlowImage(), destination)

    assert destination.read_bytes() == b"complete"
    assert not list(tmp_path.glob(".page-1-*.png"))
