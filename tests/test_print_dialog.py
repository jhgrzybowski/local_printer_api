"""Print-dialog features: page selection, scaling, layout plans, and options."""
from __future__ import annotations

import gzip
from io import BytesIO
from pathlib import Path
from typing import Any, Iterator

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, FloatObject, NameObject

from app.main import app, get_cups_client, get_file_storage
from app.services.cups_client import PrinterProfile
from app.services.database import Database
from app.services.file_storage import TempFileStorage
from app.services.ppd import parse_ppd
from tests.helpers import signup_user
from tests.test_print_api import FakeCupsClient
from tests.test_print_options import MG5350_QUEUE

PPD = parse_ppd((Path(__file__).parent / "fixtures" / "gutenprint_mg5350_excerpt.ppd").read_text(encoding="utf-8"))


class DialogCupsClient(FakeCupsClient):
    """The real MG5350 queue's capabilities, PPD labels, and IPP attributes."""

    def __init__(self, ppd: Any = PPD) -> None:
        super().__init__()
        self.ppd = ppd
        self.printed_pages: list[list[float]] = []
        self.printed_streams: list[bytes] = []

    def get_option_capabilities(self) -> dict[str, set[str]]:
        capabilities = {**self.ppd.capabilities(), **{k: v for k, v in MG5350_QUEUE.items() if k[0].islower()}}
        return {name: set(values) for name, values in capabilities.items()}

    def get_printer_profile(self) -> PrinterProfile:
        return PrinterProfile(self.get_option_capabilities(), self.ppd, {"print-quality-default": 4})

    def print_file(self, path: Path, title: str, options: dict[str, str]) -> int:
        if path.suffix == ".pdf" or path.read_bytes()[:5] == b"%PDF-":
            reader = PdfReader(path)
            self.printed_pages.append([float(page.mediabox.width) for page in reader.pages])
            contents = reader.pages[0].get_contents()
            self.printed_streams.append(contents.get_data() if contents is not None else b"")
        return super().print_file(path, title, options)


@pytest.fixture
def storage(tmp_path: Path) -> TempFileStorage:
    return TempFileStorage(tmp_path, max_upload_mb=1)


@pytest.fixture
def cups() -> DialogCupsClient:
    return DialogCupsClient()


@pytest.fixture
def client(storage: TempFileStorage, cups: DialogCupsClient, isolated_database: Database) -> Iterator[TestClient]:
    from tests.test_print_api import grant_cups_job

    app.dependency_overrides.clear()
    app.dependency_overrides[get_file_storage] = lambda: storage
    app.dependency_overrides[get_cups_client] = lambda: cups
    with TestClient(app) as test_client:
        user = signup_user(test_client)
        grant_cups_job(isolated_database, 123, user["user"]["id"])
        yield test_client
    app.dependency_overrides.clear()


def numbered_pdf(widths: list[int], link: bool = False) -> bytes:
    """Pages whose width encodes their number, so order is checkable."""
    writer = PdfWriter()
    for width in widths:
        page = writer.add_blank_page(width=width, height=842)
        drawing = DecodedStreamObject()
        drawing.set_data(f"0 g 10 10 {width - 20} 822 re f".encode())
        page[NameObject("/Contents")] = writer._add_object(drawing)
        if link:
            annotation = DictionaryObject({
                NameObject("/Type"): NameObject("/Annot"),
                NameObject("/Subtype"): NameObject("/Link"),
                NameObject("/Rect"): ArrayObject([FloatObject(v) for v in (0, 0, 100, 100)]),
            })
            page[NameObject("/Annots")] = ArrayObject([writer._add_object(annotation)])
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def upload(client: TestClient, content: bytes, name: str = "doc.pdf", mime: str = "application/pdf") -> str:
    response = client.post("/files", files={"file": (name, content, mime)})
    assert response.status_code == 200, response.text
    return str(response.json()["file_id"])


def test_validate_returns_a_sheet_plan_without_writing_files(
    client: TestClient, storage: TempFileStorage,
) -> None:
    file_id = upload(client, numbered_pdf([595, 596, 597, 598, 599]))

    response = client.post("/print/validate", json={"file_id": file_id, "options": {
        "pages": "1-5", "page_set": "odd", "pages_per_sheet": 2, "duplex": "long-edge",
        "paper_size": "A4", "copies": 2, "scale_percent": None,
    }})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["valid"] is True
    assert body["selected_pages"] == [1, 3, 5]
    layout = body["layout"]
    assert layout["paper"] == {"keyword": "A4", "label": "A4", "width_pt": 595.0, "height_pt": 842.0}
    assert [[cell["page"] for cell in side["cells"]] for side in layout["sides"]] == [[1, 3], [5]]
    assert (layout["sheets_per_copy"], layout["total_sheets"]) == (1, 2)
    # A dry run must stay cheap: no filtered PDF is spooled.
    assert not storage.filtered_dir.exists() or list(storage.filtered_dir.iterdir()) == []


def test_print_odd_pages_in_requested_order(client: TestClient, cups: DialogCupsClient) -> None:
    file_id = upload(client, numbered_pdf([101, 102, 103, 104, 105]))

    response = client.post("/print", json={"file_id": file_id, "options": {"pages": "5,1-4", "page_set": "odd"}})

    assert response.status_code == 200, response.text
    assert cups.printed_pages == [[105.0, 101.0, 103.0]]


def test_even_pages_of_a_single_page_document_are_rejected(client: TestClient) -> None:
    file_id = upload(client, numbered_pdf([100]))

    response = client.post("/print/validate", json={"file_id": file_id, "options": {"page_set": "even"}})

    assert response.status_code == 400
    assert "No pages match" in response.json()["detail"]


def test_custom_scale_wraps_content_and_keeps_page_size(client: TestClient, cups: DialogCupsClient) -> None:
    file_id = upload(client, numbered_pdf([600, 600], link=True))

    response = client.post("/print", json={"file_id": file_id, "options": {"scale_percent": 50}})

    assert response.status_code == 200, response.text
    assert response.json()["applied_options"]["print-scaling"] == "none"
    assert cups.printed_pages == [[600.0, 600.0]]
    assert cups.printed_streams[0].startswith(b"q 0.500000 0 0 0.500000 150.0000 210.5000 cm")


def test_scale_page_content_moves_annotations(tmp_path: Path) -> None:
    from app.services.page_filter import write_pdf_pages

    source = tmp_path / "in.pdf"
    source.write_bytes(numbered_pdf([600], link=True))
    destination = tmp_path / "out.pdf"
    write_pdf_pages(source, destination, [1], scale=0.5)

    page = PdfReader(destination).pages[0]
    rect = [float(value) for value in page["/Annots"][0].get_object()["/Rect"]]
    assert rect == [150.0, 210.5, 200.0, 260.5]
    assert float(page.mediabox.width) == 600


def test_custom_scale_is_unsupported_for_images(client: TestClient) -> None:
    from tests.test_print_api import make_png

    file_id = upload(client, make_png(), "image.png", "image/png")

    strict = client.post("/print/validate", json={"file_id": file_id, "options": {"scale_percent": 80}})
    assert strict.status_code == 200
    assert strict.json()["unsupported_options"] == ["scale_percent"]
    assert "print-scaling" not in strict.json()["applied_options"]
    assert client.post("/print", json={"file_id": file_id, "options": {"scale_percent": 80}}).status_code == 422


def test_text_page_selection_is_passed_to_cups_without_a_font(
    client: TestClient, cups: DialogCupsClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import file_storage

    monkeypatch.setattr(file_storage, "find_text_font", lambda: None)
    file_id = upload(client, b"line one\n" * 200, "notes.txt", "text/plain")

    response = client.post("/print", json={"file_id": file_id, "options": {"pages": "2-4, 7", "page_set": "even"}})
    assert response.status_code == 200, response.text
    assert cups.submissions[-1]["options"]["page-ranges"] == "2-4,7"
    assert cups.submissions[-1]["options"]["page-set"] == "even"

    rejected = client.post("/print/validate", json={"file_id": file_id, "options": {"pages": "7,2"}})
    assert rejected.status_code == 400


def test_strict_mode_rejects_unknown_advanced_choices(client: TestClient, cups: DialogCupsClient) -> None:
    file_id = upload(client, numbered_pdf([100]))
    options = {"advanced": {"StpImageType": "Photo", "StpColorPrecision": "Ultra"}}

    strict = client.post("/print", json={"file_id": file_id, "options": options})
    assert strict.status_code == 422
    assert "advanced.StpColorPrecision" in strict.json()["detail"]

    relaxed = client.post("/print", json={"file_id": file_id, "options": options, "strict_options": False})
    assert relaxed.status_code == 200
    assert relaxed.json()["applied_options"]["StpImageType"] == "Photo"
    assert "StpColorPrecision" not in relaxed.json()["applied_options"]


def test_ppd_constraints_block_conflicting_choices(storage: TempFileStorage, isolated_database: Database) -> None:
    constrained = parse_ppd(
        "*OpenUI *Duplex/Two-Sided: PickOne\n*Duplex None/Off: \"\"\n*Duplex DuplexNoTumble/Long: \"\"\n"
        "*CloseUI: *Duplex\n*OpenUI *MediaType/Paper: PickOne\n*MediaType Plain/Plain: \"\"\n"
        "*MediaType Envelope/Envelope: \"\"\n*CloseUI: *MediaType\n"
        "*UIConstraints: *Duplex *MediaType Envelope\n"
    )
    class PpdOnly(DialogCupsClient):
        def get_option_capabilities(self) -> dict[str, set[str]]:
            return constrained.capabilities()

    cups = PpdOnly(constrained)
    app.dependency_overrides.clear()
    app.dependency_overrides[get_file_storage] = lambda: storage
    app.dependency_overrides[get_cups_client] = lambda: cups
    with TestClient(app) as client:
        signup_user(client)
        file_id = upload(client, numbered_pdf([100]))
        options = {"duplex": "long-edge", "advanced": {"MediaType": "Envelope"}}

        validation = client.post("/print/validate", json={"file_id": file_id, "options": options})
        printed = client.post("/print", json={"file_id": file_id, "options": options})
    app.dependency_overrides.clear()

    assert validation.json()["valid"] is False
    assert validation.json()["conflicts"] == ["Duplex=DuplexNoTumble cannot be combined with MediaType=Envelope"]
    assert printed.status_code == 422
    assert cups.submissions == []


def test_options_endpoint_lists_labels_advanced_settings_and_is_compressed(client: TestClient) -> None:
    response = client.get("/options", headers={"Accept-Encoding": "gzip"})

    assert response.status_code == 200
    assert response.headers["content-encoding"] == "gzip"
    body = response.json()
    assert body["media_sources"]["labels"]["Cassette"] == "Cassette"
    assert body["paper_sizes"]["labels"]["w288h432"] == "4x6"
    assert body["defaults"]["quality"] == "normal"
    assert "Resolution" in {option["keyword"] for option in body["advanced"]}
    raw = client.get("/options", headers={"Accept-Encoding": "identity"})
    assert "content-encoding" not in raw.headers
    assert len(gzip.compress(raw.content)) < len(raw.content)


def test_preview_list_reports_page_sizes_for_layout(client: TestClient) -> None:
    file_id = upload(client, numbered_pdf([300, 842]))

    pages = client.get(f"/files/{file_id}/preview").json()["pages"]

    assert [(page["width_pt"], page["height_pt"]) for page in pages] == [(300.0, 842.0), (842.0, 842.0)]
    assert pages[0]["thumb_url"] == f"/files/{file_id}/preview/1?size=thumb"


@pytest.mark.parametrize(("options", "resolution"), [
    ({"quality": "high"}, "600dpi"),
    ({"quality": "high", "media_type": "glossy"}, "605x600dpi"),
    ({"quality": "high", "duplex": "long-edge"}, "603x600dpi"),
    ({"quality": "high", "advanced": {"Resolution": "300dpi"}}, "300dpi"),
    ({"quality": "normal"}, None),
])
def test_high_quality_selects_the_driver_high_mode(
    client: TestClient, options: dict[str, object], resolution: str | None,
) -> None:
    file_id = upload(client, numbered_pdf([100]))

    applied = client.post("/print/validate", json={"file_id": file_id, "options": options}).json()["applied_options"]

    assert applied.get("Resolution") == resolution
    assert applied["print-quality"] == ("5" if options["quality"] == "high" else "4")


@pytest.mark.parametrize(("duplex", "resolution"), [(None, "603x600dpi"), ("none", "600dpi")])
def test_high_quality_follows_the_queue_default_sides(
    storage: TempFileStorage, isolated_database: Database, duplex: str | None, resolution: str,
) -> None:
    class DuplexDefaultQueue(DialogCupsClient):
        def get_printer_profile(self) -> PrinterProfile:
            return PrinterProfile(self.get_option_capabilities(), self.ppd, {"sides-default": "two-sided-long-edge"})

    app.dependency_overrides.clear()
    app.dependency_overrides[get_file_storage] = lambda: storage
    app.dependency_overrides[get_cups_client] = lambda: DuplexDefaultQueue()
    with TestClient(app) as test_client:
        signup_user(test_client)
        file_id = upload(test_client, numbered_pdf([100]))
        options = {"quality": "high", **({"duplex": duplex} if duplex else {})}
        applied = test_client.post(
            "/print/validate", json={"file_id": file_id, "options": options},
        ).json()["applied_options"]
    app.dependency_overrides.clear()

    assert applied["Resolution"] == resolution


def test_text_upload_renders_a_unicode_pdf_for_preview_and_print(
    client: TestClient, cups: DialogCupsClient,
) -> None:
    from app.services.text_render import find_text_font

    if find_text_font() is None:
        pytest.skip("no Unicode monospace font installed (set TEXT_FONT_PATH)")
    text = "Zażółć gęślą jaźń\n" + "\n".join(f"line {n}" for n in range(2, 131))
    response = client.post("/files", files={"file": ("notatki.txt", text.encode(), "text/plain")})
    body = response.json()

    assert (body["converted"], body["preview_available"], body["page_count"]) == (True, True, 3)
    assert body["printable_mime"] == "application/pdf"
    reader = PdfReader(BytesIO(client.get(f"/files/{body['file_id']}/pdf").content))
    assert reader.pages[0].extract_text().startswith("Zażółć gęślą jaźń")
    assert client.get(f"/files/{body['file_id']}/preview/2?size=thumb").status_code == 200
    printed = client.post("/print", json={"file_id": body["file_id"], "options": {"pages": "3,1"}})
    assert printed.status_code == 200, printed.text
    assert "page-ranges" not in printed.json()["applied_options"]
    assert len(cups.printed_pages[-1]) == 2
