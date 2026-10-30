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
        {"page": 1, "url": f"/files/{file_id}/preview/1",
         "thumb_url": f"/files/{file_id}/preview/1?size=thumb", "width_pt": 72.0, "height_pt": 72.0},
        {"page": 2, "url": f"/files/{file_id}/preview/2",
         "thumb_url": f"/files/{file_id}/preview/2?size=thumb", "width_pt": 72.0, "height_pt": 72.0},
    ]


def test_pdf_preview_renders_only_requested_page(
    file_client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from PIL import Image
    from app.services import preview

    rendered_pages: list[tuple[int, bool]] = []

    def render_page(source: Path, page: int, **kwargs: object) -> Image.Image:
        assert Path(source).exists()
        assert kwargs["dpi"] == 110
        rendered_pages.append((page, bool(kwargs["exact"])))
        size = 4 if not kwargs["exact"] else 2
        return Image.new("RGB", (size, size), color="white")

    monkeypatch.setattr(preview, "render_pdf_page", render_page)
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
    assert metadata.json()["pages"][0]["width_pt"] == 72.0
    assert rendered_pages == []
    # Listing reads page sizes once; no page is rendered yet.
    assert sorted(path.name for path in preview_dir.iterdir()) == ["geometry.json"]

    page = file_client.get(f"/files/{file_id}/preview/2")
    assert page.status_code == 200
    assert page.headers["content-type"] == "image/png"
    assert "immutable" in page.headers["cache-control"]
    assert rendered_pages == [(2, False)]
    assert sorted(path.name for path in preview_dir.iterdir()) == ["geometry.json", "page-2.png"]

    assert file_client.get(f"/files/{file_id}/preview/2").status_code == 200
    assert rendered_pages == [(2, False)]
    metadata = file_client.get(f"/files/{file_id}/preview")
    assert metadata.json()["pages"][1]["size_bytes"] == len(page.content)
    assert "size_bytes" not in metadata.json()["pages"][0]

    thumb = file_client.get(f"/files/{file_id}/preview/2?size=thumb")
    assert thumb.status_code == 200
    assert rendered_pages == [(2, False), (2, True)]
    assert len(thumb.content) < len(page.content)
    assert file_client.get(f"/files/{file_id}/preview/2?size=huge").status_code == 400


def test_concurrent_requests_render_a_page_once(
    file_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from PIL import Image
    from app.services import preview

    calls: list[int] = []
    release = threading.Event()

    def slow_render(source: Path, page: int, **kwargs: object) -> Image.Image:
        calls.append(page)
        release.wait(2)
        return Image.new("RGB", (4, 4), color="white")

    monkeypatch.setattr(preview, "render_pdf_page", slow_render)
    file_id = file_client.post(
        "/files", files={"file": ("one.pdf", make_pdf(1), "application/pdf")},
    ).json()["file_id"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(file_client.get, f"/files/{file_id}/preview/1") for _ in range(4)]
        threading.Timer(0.3, release.set).start()
        statuses = [future.result().status_code for future in futures]

    assert statuses == [200, 200, 200, 200]
    assert calls == [1]


def test_pdf_preview_renders_with_poppler(file_client: TestClient) -> None:
    import shutil
    from PIL import Image

    if shutil.which("pdftoppm") is None:
        pytest.skip("poppler is not installed")
    file_id = file_client.post(
        "/files", files={"file": ("one.pdf", make_pdf(1), "application/pdf")},
    ).json()["file_id"]

    full = file_client.get(f"/files/{file_id}/preview/1")
    thumb = file_client.get(f"/files/{file_id}/preview/1?size=thumb")

    assert full.status_code == 200 and thumb.status_code == 200
    # A 1-inch page renders at PREVIEW_DPI; a thumbnail's long side is 320 px.
    assert Image.open(BytesIO(full.content)).size == (110, 110)
    assert Image.open(BytesIO(thumb.content)).size == (320, 320)


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
    assert sanitize_filename(".docx") == "upload.docx"
    assert sanitize_filename("....docx") == "upload.docx"


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
        def save(self, path: Path, image_format: str, **_: object) -> None:
            assert image_format == "PNG"
            path.write_bytes(b"partial")
            assert not destination.exists()
            path.write_bytes(b"complete")

    _save_png_atomic(SlowImage(), destination)

    assert destination.read_bytes() == b"complete"
    assert not list(tmp_path.glob(".page-1-*.png"))


def test_oversized_pdf_pages_are_capped_inside_poppler(
    file_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from PIL import Image
    from app.services import preview

    calls: list[bool] = []

    def render_page(source: Path, page: int, **kwargs: object) -> Image.Image:
        calls.append(bool(kwargs["exact"]))
        return Image.new("RGB", (4, 4), color="white")

    monkeypatch.setattr(preview, "render_pdf_page", render_page)
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)  # A4 renders at PREVIEW_DPI
    writer.add_blank_page(width=2384, height=3370)  # A0 would be 3641x5148 px
    buffer = BytesIO()
    writer.write(buffer)
    file_id = file_client.post(
        "/files", files={"file": ("mixed.pdf", buffer.getvalue(), "application/pdf")},
    ).json()["file_id"]

    assert file_client.get(f"/files/{file_id}/preview/1").status_code == 200
    assert file_client.get(f"/files/{file_id}/preview/2").status_code == 200
    assert calls == [False, True]


def test_concurrent_image_previews_render_once(
    file_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from app.services.preview import PreviewService

    calls: list[int] = []
    release = threading.Event()
    original = PreviewService._render_image

    def slow_render(self: PreviewService, source: Path, output: Path, dimension: int) -> Path:
        calls.append(dimension)
        release.wait(2)
        return original(self, source, output, dimension)

    monkeypatch.setattr(PreviewService, "_render_image", slow_render)
    file_id = file_client.post(
        "/files", files={"file": ("image.png", make_png(), "image/png")},
    ).json()["file_id"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(file_client.get, f"/files/{file_id}/preview") for _ in range(4)]
        threading.Timer(0.3, release.set).start()
        statuses = [future.result().status_code for future in futures]

    assert statuses == [200, 200, 200, 200]
    assert len(calls) == 1


def test_unwritable_geometry_cache_does_not_fail_validation(
    file_client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import preview

    def fail_write(destination: Path, data: bytes) -> None:
        raise PermissionError("read-only preview directory")

    monkeypatch.setattr(preview, "_write_atomic", fail_write)
    file_id = file_client.post(
        "/files", files={"file": ("two.pdf", make_pdf(2), "application/pdf")},
    ).json()["file_id"]

    listing = file_client.get(f"/files/{file_id}/preview")

    assert listing.status_code == 200
    assert listing.json()["pages"][0]["width_pt"] == 72.0
