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
from app.services.layout import PageBox, Paper, plan_layout
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


@pytest.mark.skipif(shutil.which("cupsfilter") is None, reason="cupsfilter is not installed")
@pytest.mark.parametrize(("number_up", "orientation"), [(1, None), (2, None), (4, None), (6, None), (2, "4")])
def test_layout_plan_matches_cups_page_count_and_sheet_shape(
    tmp_path: Path, number_up: int, orientation: str | None,
) -> None:
    profile = CupsClient(QUEUE or "").get_printer_profile()
    assert profile.ppd is not None
    sizes = [(595, 842), (842, 595), (595, 842), (612, 792)]
    source = tmp_path / "mixed.pdf"
    source.write_bytes(pdf(sizes))
    ppd_path = tmp_path / "queue.ppd"
    status, _, filename = CupsClient(QUEUE or "")._connection().getPPD3(QUEUE)
    shutil.move(filename, ppd_path)
    args = ["cupsfilter", "-p", str(ppd_path), "-m", "application/vnd.cups-pdf",
            "-o", "PageSize=A4", "-o", f"number-up={number_up}"]
    if orientation:
        args += ["-o", f"orientation-requested={orientation}"]
    output = subprocess.run(args + [str(source)], capture_output=True, check=True).stdout
    filtered = PdfReader(BytesIO(output))

    plan = plan_layout(
        [PageBox(index + 1, width, height) for index, (width, height) in enumerate(sizes)],
        Paper("A4", "A4", 595, 842), pages_per_sheet=number_up,
        orientation="landscape" if orientation == "4" else None,
    )
    assert plan["impressions_per_copy"] == len(filtered.pages)
    if number_up > 1:
        first = filtered.pages[0]
        width, height = float(first.mediabox.width), float(first.mediabox.height)
        if first.rotation % 180:
            width, height = height, width
        side = plan["sides"][0]  # type: ignore[index]
        # The plan may show a sheet turned so its pages read upright.
        assert {round(width), round(height)} == {round(side["width_pt"]), round(side["height_pt"])}
