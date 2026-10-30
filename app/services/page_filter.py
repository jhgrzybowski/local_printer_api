from __future__ import annotations

import re
from pathlib import Path

from pypdf import PageObject, PdfReader, PdfWriter
from pypdf.errors import PdfReadError
from pypdf.generic import ArrayObject, DecodedStreamObject, FloatObject, NameObject


RANGE_RE = re.compile(r"^\d+(?:-\d+)?$")


class PageRangeError(ValueError):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def parse_page_ranges(page_ranges: str, page_count: int) -> list[int]:
    text = page_ranges.strip()
    if not text:
        raise PageRangeError("Page range is empty")
    if page_count < 1:
        raise PageRangeError("Document has no pages")

    pages: list[int] = []
    for token in text.split(","):
        token = token.strip()
        if not RANGE_RE.fullmatch(token):
            raise PageRangeError(f"Malformed page range: {token or '<empty>'}")
        if "-" in token:
            start_text, end_text = token.split("-", 1)
            start = _parse_page_number(start_text, page_count)
            end = _parse_page_number(end_text, page_count)
            if start > end:
                raise PageRangeError(f"Reversed page range: {token}")
            pages.extend(range(start, end + 1))
        else:
            pages.append(_parse_page_number(token, page_count))

    return pages


def select_pages(page_ranges: str | None, page_set: str | None, page_count: int) -> list[int]:
    """Apply a page range, then odd/even selection, keeping the given order."""
    if page_ranges:
        pages = parse_page_ranges(page_ranges, page_count)
    elif page_count < 1:
        raise PageRangeError("Document has no pages")
    else:
        pages = list(range(1, page_count + 1))
    if page_set == "odd":
        pages = [page for page in pages if page % 2 == 1]
    elif page_set == "even":
        pages = [page for page in pages if page % 2 == 0]
    if not pages:
        raise PageRangeError("No pages match the selected page range and page set")
    return pages


def filter_pdf_pages(source: Path, destination: Path, page_ranges: str, page_count: int) -> list[int]:
    pages = parse_page_ranges(page_ranges, page_count)
    write_pdf_pages(source, destination, pages)
    return pages


def write_pdf_pages(source: Path, destination: Path, pages: list[int], scale: float | None = None) -> None:
    """Write the selected pages in order, optionally scaling their content."""
    try:
        reader = PdfReader(source)
    except PdfReadError as exc:
        raise PageRangeError("Unable to read PDF for page filtering") from exc

    if reader.is_encrypted:
        raise PageRangeError("Password-protected PDFs are not supported")

    writer = PdfWriter()
    try:
        for page_number in pages:
            page = writer.add_page(reader.pages[page_number - 1])
            if scale is not None and scale != 1:
                scale_page_content(page, scale)
        with destination.open("wb") as output:
            writer.write(output)
    except Exception as exc:
        destination.unlink(missing_ok=True)
        raise PageRangeError("Unable to create filtered PDF") from exc


def scale_page_content(page: PageObject, scale: float) -> None:
    """Scale content and annotations about the page centre, keeping its size.

    pypdf's add_transformation tokenizes the whole content stream. Wrapping the
    decoded bytes in q/cm/Q does the same job without parsing them.
    """
    box = page.mediabox
    left, bottom = float(box.left), float(box.bottom)
    right, top = float(box.right), float(box.top)
    offset_x = (left + right) / 2 * (1 - scale)
    offset_y = (bottom + top) / 2 * (1 - scale)
    contents = page.get_contents()
    if contents is not None:
        wrapped = DecodedStreamObject()
        matrix = f"q {scale:.6f} 0 0 {scale:.6f} {offset_x:.4f} {offset_y:.4f} cm\n".encode()
        wrapped.set_data(matrix + contents.get_data() + b"\nQ\n")
        page.replace_contents(wrapped.flate_encode())
    for reference in page.get("/Annots") or []:
        annotation = reference.get_object()
        rect = annotation.get("/Rect")
        if not isinstance(rect, ArrayObject) or len(rect) != 4:
            continue
        annotation[NameObject("/Rect")] = ArrayObject(
            FloatObject(round(float(value) * scale + (offset_x if index % 2 == 0 else offset_y), 4))
            for index, value in enumerate(rect)
        )


def _parse_page_number(text: str, page_count: int) -> int:
    try:
        page = int(text)
    except ValueError as exc:
        raise PageRangeError(f"Malformed page number: {text}") from exc
    if page < 1:
        raise PageRangeError(f"Invalid page number: {page}")
    if page > page_count:
        raise PageRangeError(f"Page {page} is outside document bounds")
    return page
