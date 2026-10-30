from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.models.print_options import CupsOptionMapping, PrintOptions
from app.services.cups_client import CupsClient, CupsClientError, PrinterProfile
from app.services.file_storage import StoredFile, TempFileStorage
from app.services.layout import PageBox, Paper, plan_layout
from app.services.page_filter import PageRangeError, parse_page_ranges, select_pages, write_pdf_pages
from app.services.ppd import paper_size
from app.services.preview import PreviewError, PreviewService
from app.services.printer_reachability import PROBE_FRESH_SECONDS
from app.services.status_translator import translate_queue_status

IMAGE_MIME_TYPES = {"image/png", "image/jpeg"}
PAGE_RANGE_TEXT_RE = re.compile(r"^\s*\d+(?:\s*-\s*\d+)?(?:\s*,\s*\d+(?:\s*-\s*\d+)?)*\s*$")


class PrintRequestError(ValueError):
    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class PageSelection:
    """Pages to print, decided without touching the document bytes."""

    # Pages in print order; None when the page count is unknown (text).
    pages: list[int] | None
    # True when the PDF must be rewritten (subset, reorder, or scaling).
    rewrite: bool
    # Selection that CUPS applies itself, for documents we cannot split.
    cups_options: dict[str, str] = field(default_factory=dict)
    # Reported to clients: the explicit selection, or None for all pages.
    selected_pages: list[int] | None = None


@dataclass(frozen=True)
class PreparedPrint:
    file_path: Path
    title: str
    selected_pages: list[int] | None
    temporary: bool = False
    cups_options: dict[str, str] = field(default_factory=dict)


def ensure_printer_ready(client: CupsClient) -> dict[str, object]:
    try:
        # Status and validation may show a reachability result up to a minute
        # old; a print decision needs a recent one.
        queue = client.get_queue(probe_max_age=PROBE_FRESH_SECONDS)
    except CupsClientError as exc:
        raise PrintRequestError(str(exc), 503) from exc

    status = translate_queue_status(queue)
    if not status["exists"]:
        raise PrintRequestError(f"Queue {client.queue_name} is missing", 503)
    reasons = [str(reason).lower() for reason in status.get("reasons", [])]
    if status["state"] == "offline":
        raise PrintRequestError(f"Printer appears offline: {', '.join(reasons)}", 503)
    if status["state"] == "stopped" or status["enabled"] is False:
        raise PrintRequestError(f"Queue {client.queue_name} is stopped", 409)
    if status["accepting_jobs"] is False:
        raise PrintRequestError(f"Queue {client.queue_name} is not accepting jobs", 409)
    if not status["ready_for_print"]:
        raise PrintRequestError(f"Queue {client.queue_name} readiness is unknown", 503)
    return status


def plan_selection(record: StoredFile, options: PrintOptions) -> PageSelection:
    page_set = options.page_set if options.page_set in {"odd", "even"} else None
    explicit = bool(options.pages) or page_set is not None
    try:
        if record.printable_mime == "application/pdf":
            if record.page_count is None:
                raise PrintRequestError("Cannot filter PDF without a known page count", 400)
            pages = select_pages(options.pages, page_set, record.page_count)
            rewrite = explicit or options.scale_percent is not None
            return PageSelection(pages, rewrite, selected_pages=pages if explicit else None)
        if record.detected_mime in IMAGE_MIME_TYPES:
            pages = select_pages(options.pages, page_set, 1)
            if pages != [1]:
                raise PrintRequestError("Page ranges for image files may only select page 1", 400)
            return PageSelection(pages, False, selected_pages=pages if explicit else None)
    except PageRangeError as exc:
        raise PrintRequestError(exc.message, 400) from exc
    return _text_selection(options, page_set)


def _text_selection(options: PrintOptions, page_set: str | None) -> PageSelection:
    """Plain text is paginated by CUPS, so CUPS also selects its pages."""
    cups_options: dict[str, str] = {}
    if options.pages:
        if not PAGE_RANGE_TEXT_RE.fullmatch(options.pages):
            raise PrintRequestError(f"Malformed page range: {options.pages}", 400)
        # Bounds are unknown until CUPS lays out the text; only order is checked.
        try:
            ranges = parse_page_ranges(options.pages.replace(" ", ""), 1_000_000)
        except PageRangeError as exc:
            raise PrintRequestError(exc.message, 400) from exc
        if ranges != sorted(set(ranges)):
            raise PrintRequestError("Text files support only ascending, non-overlapping page ranges", 400)
        cups_options["page-ranges"] = options.pages.replace(" ", "")
    if page_set is not None:
        cups_options["page-set"] = page_set
    return PageSelection(None, False, cups_options)


def prepare_print_file(
    storage: TempFileStorage,
    record: StoredFile,
    options: PrintOptions,
    selection: PageSelection | None = None,
) -> PreparedPrint:
    source = storage.printable_path(record)
    if not source.exists():
        raise PrintRequestError("Stored file is missing", 404)
    selection = selection or plan_selection(record, options)
    if not selection.rewrite or selection.pages is None:
        return PreparedPrint(source, record.original_filename, selection.selected_pages,
                             cups_options=selection.cups_options)

    filtered = storage.filtered_pdf_path(record.file_id)
    scale = options.scale_percent / 100 if options.scale_percent is not None else None
    try:
        write_pdf_pages(source, filtered, selection.pages, scale=scale)
    except PageRangeError as exc:
        raise PrintRequestError(exc.message, 400) from exc
    title = record.original_filename
    if options.pages:
        title = f"{title} pages {options.pages}"
    return PreparedPrint(filtered, title, selection.selected_pages, True, selection.cups_options)


def map_options(
    record: StoredFile, options: PrintOptions, profile: PrinterProfile,
) -> tuple[CupsOptionMapping, list[str]]:
    """Map options for this document; return the mapping and PPD conflicts."""
    mapped = options.to_cups_options(dict(profile.capabilities))
    applied = dict(mapped.applied_options)
    unsupported = list(mapped.unsupported_options)
    warnings = list(mapped.warnings)
    if options.scale_percent is not None and record.printable_mime != "application/pdf" \
            and "scale_percent" not in unsupported:
        # Only PDF pages can be scaled here before CUPS receives them.
        applied.pop("print-scaling", None)
        unsupported.append("scale_percent")
        warnings.append("Dropped scale_percent because custom scaling needs a PDF document")
    conflicts = profile.ppd.conflicts(applied) if profile.ppd is not None else []
    return CupsOptionMapping(applied, unsupported, warnings), conflicts


def submit_print_job(
    client: CupsClient,
    storage: TempFileStorage,
    record: StoredFile,
    options: PrintOptions,
    strict_options: bool = True,
) -> dict[str, object]:
    ensure_printer_ready(client)
    selection = plan_selection(record, options)
    try:
        profile = client.get_printer_profile()
    except CupsClientError as exc:
        raise PrintRequestError(str(exc), 503) from exc
    if strict_options and not profile.capabilities:
        raise PrintRequestError("Printer capabilities are unavailable; cannot validate options", 503)
    mapped, conflicts = map_options(record, options, profile)
    if strict_options and mapped.unsupported_options:
        raise PrintRequestError(f"Unsupported options: {', '.join(mapped.unsupported_options)}", 422)
    if strict_options and conflicts:
        raise PrintRequestError(f"Conflicting options: {'; '.join(conflicts)}", 422)

    prepared = prepare_print_file(storage, record, options, selection)
    applied = {**mapped.applied_options, **prepared.cups_options}
    try:
        title = _safe_title(prepared.title)
        job_id = client.print_file(prepared.file_path, title, applied)
    except CupsClientError as exc:
        raise PrintRequestError(str(exc), 503) from exc
    finally:
        if prepared.temporary:
            prepared.file_path.unlink(missing_ok=True)

    warnings = [*record.warnings, *mapped.warnings, *conflicts]
    if prepared.selected_pages is not None:
        warnings.append("Printed pages preserve the user-specified page order")

    return {
        "job_id": job_id,
        "queue": client.queue_name,
        "submitted_filename": title,
        "applied_options": applied,
        "unsupported_options": mapped.unsupported_options,
        "warnings": warnings,
    }


def validate_print_job(
    client: CupsClient,
    storage: TempFileStorage,
    record: StoredFile,
    options: PrintOptions,
    strict_options: bool = True,
) -> dict[str, object]:
    """Dry-run a print: same checks and mapping, plus the sheet layout.

    No print file is spooled; a print dialog calls this on every settings change.
    """
    if not storage.printable_path(record).exists():
        raise PrintRequestError("Stored file is missing", 404)
    selection = plan_selection(record, options)
    try:
        profile = client.get_printer_profile()
    except CupsClientError as exc:
        raise PrintRequestError(str(exc), 503) from exc
    if not profile.capabilities and strict_options:
        raise PrintRequestError("Printer capabilities are unavailable; cannot validate options", 503)
    mapped, conflicts = map_options(record, options, profile)
    try:
        status = translate_queue_status(client.get_queue())
    except CupsClientError as exc:
        raise PrintRequestError(str(exc), 503) from exc
    try:
        layout = build_layout(storage, record, options, selection, mapped, profile)
    except PreviewError:
        layout = None
    return {
        "file_id": record.file_id,
        "ready_for_print": status["ready_for_print"],
        "valid": not mapped.unsupported_options and not conflicts,
        "page_count": record.page_count,
        "selected_pages": selection.selected_pages,
        "applied_options": {**mapped.applied_options, **selection.cups_options},
        "unsupported_options": mapped.unsupported_options,
        "conflicts": conflicts,
        "warnings": [*record.warnings, *mapped.warnings, *conflicts,
                     *(["Printed pages preserve the user-specified page order"]
                       if selection.selected_pages is not None else [])],
        "printable_sha256": record.printable_sha256,
        "layout": layout,
    }


def build_layout(
    storage: TempFileStorage,
    record: StoredFile,
    options: PrintOptions,
    selection: PageSelection,
    mapped: CupsOptionMapping,
    profile: PrinterProfile,
) -> dict[str, Any] | None:
    """Plan sheets from what will actually be applied, not what was asked."""
    if selection.pages is None:
        return None
    geometry = PreviewService(storage).page_geometry(record)
    if not geometry:
        return None
    pages = [
        PageBox(number, float(geometry[number - 1]["width_pt"]), float(geometry[number - 1]["height_pt"]))
        for number in selection.pages
        if 0 < number <= len(geometry)
    ]
    dropped = set(mapped.unsupported_options)
    applied = mapped.applied_options
    paper = _paper(applied.get("PageSize") or applied.get("media") or _default_paper(profile), profile, pages)
    if options.scale_percent is not None and "scale_percent" not in dropped:
        scaling, scale_percent = "none", options.scale_percent
    elif options.scaling is not None and "scaling" not in dropped:
        scaling, scale_percent = options.scaling, None
    elif options.fit_to_page and "fit_to_page" not in dropped:
        scaling, scale_percent = "fit", None
    else:
        scaling, scale_percent = None, None
    return plan_layout(
        pages,
        paper,
        orientation=options.orientation,
        pages_per_sheet=int(applied.get("number-up", 1)),
        pages_per_sheet_layout=applied.get("number-up-layout"),
        page_border=applied.get("page-border"),
        scaling=scaling,
        scale_percent=scale_percent,
        duplex=options.duplex if "duplex" not in dropped else None,
        copies=options.copies,
        borderless=bool(options.borderless) and "borderless" not in dropped,
        reverse_order=applied.get("page-delivery") == "reverse-order",
    )


def _default_paper(profile: PrinterProfile) -> str | None:
    if profile.ppd is not None and "PageSize" in profile.ppd.options:
        return profile.ppd.options["PageSize"].default
    return None


def _paper(keyword: str | None, profile: PrinterProfile, pages: list[PageBox]) -> Paper:
    size = paper_size(keyword, profile.ppd) if keyword else None
    if size is None:
        # Unknown paper: assume each sheet matches the first page.
        first = pages[0] if pages else PageBox(1, 595, 842)
        return Paper(keyword or "document", keyword or "Document size", first.width, first.height)
    margins = (0.0, 0.0, 0.0, 0.0)
    if size.imageable is not None:
        left, bottom, right, top = size.imageable
        margins = (max(0.0, left), max(0.0, bottom), max(0.0, size.width - right), max(0.0, size.height - top))
    return Paper(size.keyword, size.label, size.width, size.height, margins)


def _safe_title(title: str) -> str:
    cleaned = title.replace("\n", " ").replace("\r", " ").strip()
    return cleaned[:120] or f"print-{secrets.token_hex(4)}"
