"""Checks against a real CUPS queue. They print, so they only run when asked.

    CUPS_INTEGRATION_QUEUE=Canon_MG5350 python -m pytest tests/test_cups_integration.py

Use the Docker testbed in testbed/ (a fake LPD printer behind the real
Gutenprint MG5350 driver) or a queue whose output you are ready to receive.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from io import BytesIO
from pathlib import Path

import pytest
from pypdf import PdfReader, PdfWriter

from app.models.print_options import PrintOptions
from app.services.cups_client import CupsClient
from app.services.options_summary import build_options_summary

QUEUE = os.getenv("CUPS_INTEGRATION_QUEUE")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not QUEUE, reason="set CUPS_INTEGRATION_QUEUE to run real CUPS checks"),
]


def pdf(sizes: list[tuple[int, int]]) -> bytes:
    writer = PdfWriter()
    for width, height in sizes:
        writer.add_blank_page(width=width, height=height)
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def test_profile_reads_driver_labels_and_ipp_job_attributes() -> None:
    profile = CupsClient(QUEUE or "").get_printer_profile()

    assert profile.ppd is not None and profile.ppd.options
    assert "PageSize" in profile.capabilities
    assert "number-up" in profile.capabilities
    summary = build_options_summary(QUEUE or "", dict(profile.capabilities), ppd=profile.ppd, ipp=profile.ipp)
    assert summary["paper_sizes"]["details"]
    assert summary["pages_per_sheet"]["supported"] is True


def test_dialog_options_print_through_the_driver(tmp_path: Path) -> None:
    client = CupsClient(QUEUE or "")
    capabilities = client.get_option_capabilities()
    options = PrintOptions(
        paper_size="A4", pages_per_sheet=2, duplex="long-edge", quality="high",
        color_mode="monochrome", reverse_order=True, scaling="fit",
    )
    mapped = options.to_cups_options(capabilities)
    assert mapped.unsupported_options == []
    source = tmp_path / "three.pdf"
    source.write_bytes(pdf([(595, 842)] * 3))

    job_id = client.print_file(source, "integration", mapped.applied_options)
    deadline = time.monotonic() + 120
    job = client.get_job(job_id)
    while job is not None and job["is_active"] and time.monotonic() < deadline:
        time.sleep(0.2)
        job = client.get_job(job_id)

    assert job is not None and job["state"] == "completed"
    assert (job["pages_completed"] or 0) >= 1


GRAYS = (60, 110, 160, 210)


def marked_pdf(sizes: list[tuple[int, int]]) -> bytes:
    """Each page is a distinct grey with a black square in its top-left corner."""
    from pypdf.generic import DecodedStreamObject, NameObject

    writer = PdfWriter()
    for (width, height), grey in zip(sizes, GRAYS):
        page = writer.add_blank_page(width=width, height=height)
        mark = 0.25 * min(width, height)
        stream = DecodedStreamObject()
        stream.set_data(
            f"{grey / 255:.3f} g 0 0 {width} {height} re f 0 g 0 {height - mark} {mark} {mark} re f".encode()
        )
        page[NameObject("/Contents")] = writer._add_object(stream)
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def placed_pages(png: Path) -> dict[int, tuple[float, float, int]]:
    """Centre (fraction of sheet) and clockwise rotation of each page in a render."""
    from PIL import Image

    image = Image.open(png).convert("L")
    width, height = image.size
    pixels = image.load()
    found: dict[int, tuple[float, float, int]] = {}
    for number, grey in enumerate(GRAYS, start=1):
        cells = [(x, y) for y in range(height) for x in range(width) if abs(pixels[x, y] - grey) <= 12]
        if len(cells) < 20:
            continue
        left, right = min(x for x, _ in cells), max(x for x, _ in cells)
        top, bottom = min(y for _, y in cells), max(y for _, y in cells)
        marks = [(x, y) for y in range(top, bottom + 1) for x in range(left, right + 1) if pixels[x, y] < 40]
        mark_x = sum(x for x, _ in marks) / len(marks)
        mark_y = sum(y for _, y in marks) / len(marks)
        corner = ("t" if mark_y < (top + bottom) / 2 else "b") + ("l" if mark_x < (left + right) / 2 else "r")
        rotation = {"tl": 0, "tr": 90, "br": 180, "bl": 270}[corner]
        found[number] = ((left + right) / 2 / width, (top + bottom) / 2 / height, rotation)
    return found


def turn(point: tuple[float, float, int], degrees: int) -> tuple[float, float, int]:
    """Turn a placement clockwise with the whole sheet."""
    x, y, rotation = point
    for _ in range(degrees // 90):
        x, y = 1 - y, x
    return x, y, (rotation + degrees) % 360


@pytest.mark.skipif(shutil.which("cupsfilter") is None, reason="cupsfilter is not installed")
@pytest.mark.parametrize("settings", [
    {}, {"pages_per_sheet": 2}, {"pages_per_sheet": 4}, {"pages_per_sheet": 6},
    {"pages_per_sheet": 4, "pages_per_sheet_layout": "tblr"},
    {"orientation": "landscape"}, {"orientation": "landscape", "pages_per_sheet": 2},
    {"orientation": "portrait", "pages_per_sheet": 4}, {"scaling": "fill"},
])
def test_layout_plan_matches_where_cups_puts_each_page(tmp_path: Path, settings: dict[str, object]) -> None:
    from app.services.print_service import build_layout, map_options, plan_selection
    from app.services.file_storage import StoredFile, TempFileStorage

    client = CupsClient(QUEUE or "")
    profile = client.get_printer_profile()
    sizes = [(595, 842), (842, 595), (595, 842), (612, 792)]
    storage = TempFileStorage(tmp_path / "storage")
    storage.files_dir.mkdir(parents=True)
    record = StoredFile("integration-file-0001", "mixed.pdf", "application/pdf", 0, len(sizes), True)
    storage.file_path(record.file_id).write_bytes(marked_pdf(sizes))
    options = PrintOptions(paper_size="A4", **settings)
    mapped, conflicts = map_options(record, options, profile)
    assert mapped.unsupported_options == [] and conflicts == []
    plan = build_layout(storage, record, options, plan_selection(record, options), mapped, profile)
    assert plan is not None

    ppd_path = tmp_path / "queue.ppd"
    _, _, filename = client._connection().getPPD3(QUEUE)
    shutil.move(filename, ppd_path)
    args = ["cupsfilter", "-p", str(ppd_path), "-m", "application/vnd.cups-pdf"]
    for name, value in mapped.applied_options.items():
        args += ["-o", f"{name}={value}"]
    output = tmp_path / "cups.pdf"
    output.write_bytes(subprocess.run(
        args + [str(storage.file_path(record.file_id))], capture_output=True, check=True,
    ).stdout)
    assert plan["impressions_per_copy"] == len(PdfReader(output).pages)

    for index, side in enumerate(plan["sides"]):  # type: ignore[arg-type]
        png = tmp_path / f"side-{index}"
        subprocess.run(["pdftoppm", "-f", str(index + 1), "-l", str(index + 1), "-r", "24", "-gray",
                        "-png", "-singlefile", str(output), str(png)], check=True)
        actual = placed_pages(png.with_suffix(".png"))
        expected = {
            cell["page"]: (cell["x"] + cell["width"] / 2, cell["y"] + cell["height"] / 2, cell["rotate"])
            for cell in side["cells"] if cell["width"] * cell["height"] > 0.02
        }
        assert set(actual) == set(expected), (settings, index, actual, expected)
        # The plan shows the sheet turned so its pages read upright; find that turn.
        first = min(expected)
        sheet_turn = (expected[first][2] - actual[first][2]) % 360
        if sheet_turn not in (0, 90, 180, 270):
            sheet_turn = (360 - actual[first][2]) % 360
        for number, wanted in expected.items():
            x, y, rotation = turn(actual[number], sheet_turn)
            assert abs(x - wanted[0]) < 0.08 and abs(y - wanted[1]) < 0.08, (settings, number, (x, y), wanted)
            # The plan rotates cells by 90 without saying which way.
            assert rotation in ({90, 270} if wanted[2] else {0}), (settings, number, rotation, wanted)
