from __future__ import annotations

import secrets
from dataclasses import dataclass
from pathlib import Path

from app.models.print_options import PrintOptions
from app.services.cups_client import CupsClient, CupsClientError
from app.services.file_storage import StoredFile, TempFileStorage
from app.services.page_filter import PageRangeError, filter_pdf_pages, parse_page_ranges
from app.services.status_translator import translate_queue_status


class PrintRequestError(ValueError):
    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class PreparedPrint:
    file_path: Path
    title: str
    selected_pages: list[int] | None


def ensure_printer_ready(client: CupsClient) -> dict[str, object]:
    try:
        queue = client.get_queue()
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

    if any("offline" in reason or reason == "network-unreachable" for reason in reasons):
        raise PrintRequestError(f"Printer appears offline: {', '.join(reasons)}", 503)
    return status


def prepare_print_file(
    storage: TempFileStorage,
    record: StoredFile,
    options: PrintOptions,
) -> PreparedPrint:
    source = storage.file_path(record.file_id)
    if not source.exists():
        raise PrintRequestError("Stored file is missing", 404)

    if not options.pages:
        return PreparedPrint(source, record.original_filename, None)

    if record.detected_mime == "application/pdf":
        if record.page_count is None:
            raise PrintRequestError("Cannot filter PDF without a known page count", 400)
        filtered = storage.filtered_pdf_path(record.file_id)
        try:
            selected_pages = filter_pdf_pages(source, filtered, options.pages, record.page_count)
        except PageRangeError as exc:
            raise PrintRequestError(exc.message, 400) from exc
        return PreparedPrint(filtered, f"{record.original_filename} pages {options.pages}", selected_pages)

    if record.detected_mime in {"image/png", "image/jpeg"}:
        try:
            selected_pages = parse_page_ranges(options.pages, 1)
        except PageRangeError as exc:
            raise PrintRequestError(exc.message, 400) from exc
        if selected_pages != [1]:
            raise PrintRequestError("Page ranges for image files may only select page 1", 400)
        return PreparedPrint(source, record.original_filename, selected_pages)

    raise PrintRequestError("Page ranges are only supported for PDFs and single-page images", 400)


def submit_print_job(
    client: CupsClient,
    storage: TempFileStorage,
    record: StoredFile,
    options: PrintOptions,
) -> dict[str, object]:
    ensure_printer_ready(client)
    prepared = prepare_print_file(storage, record, options)
    capabilities = client.get_option_capabilities()
    mapped = options.to_cups_options(capabilities)
    title = _safe_title(prepared.title)

    try:
        job_id = client.print_file(prepared.file_path, title, mapped.applied_options)
    except CupsClientError as exc:
        raise PrintRequestError(str(exc), 503) from exc

    warnings = list(mapped.warnings)
    if prepared.selected_pages is not None:
        warnings.append("Printed pages preserve the user-specified page order")

    return {
        "job_id": job_id,
        "queue": client.queue_name,
        "submitted_filename": title,
        "applied_options": mapped.applied_options,
        "unsupported_options": mapped.unsupported_options,
        "warnings": warnings,
    }


def _safe_title(title: str) -> str:
    cleaned = title.replace("\n", " ").replace("\r", " ").strip()
    return cleaned[:120] or f"print-{secrets.token_hex(4)}"
