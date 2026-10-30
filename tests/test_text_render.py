from __future__ import annotations

from pathlib import Path

import pytest
from pypdf import PdfReader

from app.services import text_render
from app.services.text_render import TextRenderError, find_text_font, render_text_pdf


def test_pagination_wraps_expands_tabs_and_breaks_on_form_feed() -> None:
    pages = text_render._paginate("a\tb\n" + "x" * 85 + "\fnext\n", columns=80, rows=60)

    assert pages[0] == ["a       b", "x" * 80, "x" * 5]
    assert pages[1] == ["next"]
    assert len(pages) == 2


def test_pagination_fills_pages_without_a_trailing_blank_page() -> None:
    exact = text_render._paginate("\n".join(str(n) for n in range(1, 121)) + "\n", columns=80, rows=60)
    over = text_render._paginate("\n".join(str(n) for n in range(1, 122)), columns=80, rows=60)

    assert [len(page) for page in exact] == [60, 60]
    assert exact[1][-1] == "120"
    assert [len(page) for page in over] == [60, 60, 1]


def test_very_long_text_is_left_to_cups(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(text_render, "MAX_PAGES", 2)

    with pytest.raises(TextRenderError):
        text_render._paginate("line\n" * 500, columns=80, rows=60)


def test_rendered_pdf_embeds_the_font_and_keeps_text(tmp_path: Path) -> None:
    font = find_text_font()
    if font is None:
        pytest.skip("no Unicode monospace font installed (set TEXT_FONT_PATH)")
    source = tmp_path / "in.txt"
    source.write_text("Zażółć gęślą jaźń – Привет\n\tindented\n", encoding="utf-8")

    pages = render_text_pdf(source, tmp_path / "out.pdf", font)

    reader = PdfReader(tmp_path / "out.pdf")
    assert pages == 1
    assert [round(float(v)) for v in reader.pages[0].mediabox] == [0, 0, 595, 842]
    font_dict = reader.pages[0]["/Resources"]["/Font"]["/F1"].get_object()
    descendant = font_dict["/DescendantFonts"][0].get_object()
    assert "/FontFile2" in descendant["/FontDescriptor"].get_object()
    assert reader.pages[0].extract_text().splitlines()[0] == "Zażółć gęślą jaźń – Привет"


def test_fonts_without_truetype_outlines_are_rejected(tmp_path: Path) -> None:
    fake = tmp_path / "fake.otf"
    fake.write_bytes(b"OTTO\x00\x00\x00\x00" + b"\x00" * 8)

    with pytest.raises(TextRenderError):
        render_text_pdf(tmp_path / "missing.txt", tmp_path / "out.pdf", fake)
