from __future__ import annotations

import os
import tempfile
from pathlib import Path

from app.services.file_storage import StoredFile, TempFileStorage
from app.settings import PREVIEW_DPI


class PreviewError(RuntimeError):
    def __init__(self, message: str, status_code: int = 500) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class PreviewService:
    def __init__(self, storage: TempFileStorage, dpi: int = PREVIEW_DPI) -> None:
        self.storage = storage
        self.dpi = dpi

    def ensure_previews(self, record: StoredFile) -> list[Path]:
        if not record.preview_available:
            raise PreviewError("Preview is not available for this file type", 400)

        preview_dir = self.storage.preview_dir(record.file_id)
        expected_count = record.page_count or 1
        paths = [preview_dir / f"page-{page}.png" for page in range(1, expected_count + 1)]
        if record.printable_mime == "application/pdf":
            # Metadata requests only need page URLs. Render a PDF page when its
            # PNG is requested so a large document does not occupy a worker.
            if not self.storage.printable_path(record).exists():
                raise PreviewError("Stored file is missing", 404)
            return paths

        if paths[0].exists():
            return paths

        preview_dir.mkdir(parents=True, exist_ok=True)
        source = self.storage.printable_path(record)
        if not source.exists():
            raise PreviewError("Stored file is missing", 404)

        if record.detected_mime in {"image/png", "image/jpeg"}:
            return [self._render_image(source, preview_dir)]

        raise PreviewError("Preview is not available for this file type", 400)

    def preview_path(self, record: StoredFile, page: int) -> Path:
        if page < 1:
            raise PreviewError("Invalid page number", 400)
        if record.page_count is not None and page > record.page_count:
            raise PreviewError("Invalid page number", 400)

        self.ensure_previews(record)
        path = self.storage.preview_dir(record.file_id) / f"page-{page}.png"
        if record.printable_mime == "application/pdf" and not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            self._render_pdf(self.storage.printable_path(record), path, page)
        if not path.exists():
            raise PreviewError("Preview page not found", 404)
        return path

    def _render_pdf(self, source: Path, destination: Path, page: int) -> None:
        try:
            from pdf2image import convert_from_path
            from pdf2image.exceptions import PDFInfoNotInstalledError, PDFPageCountError, PDFPopplerTimeoutError
        except ImportError as exc:
            raise PreviewError("pdf2image is not installed") from exc

        try:
            images = convert_from_path(
                str(source), dpi=self.dpi, fmt="png", first_page=page, last_page=page, timeout=30, size=1600
            )
            if len(images) != 1:
                raise PreviewError("Unable to render PDF preview page", 400)
            with images[0] as image:
                _save_png_atomic(image, destination)
        except PDFInfoNotInstalledError as exc:
            raise PreviewError("Poppler is not installed or not available on PATH") from exc
        except PDFPopplerTimeoutError as exc:
            raise PreviewError("PDF preview exceeded its time limit", 504) from exc
        except PDFPageCountError as exc:
            raise PreviewError("Unable to inspect PDF for preview generation", 400) from exc
        except PreviewError:
            raise
        except Exception as exc:
            raise PreviewError(f"Unable to generate PDF preview: {exc}") from exc

    def _render_image(self, source: Path, preview_dir: Path) -> Path:
        try:
            from PIL import Image
        except ImportError as exc:
            raise PreviewError("Pillow is not installed") from exc

        try:
            with Image.open(source) as image:
                image.thumbnail((1600, 1600))
                output = preview_dir / "page-1.png"
                _save_png_atomic(image.convert("RGB"), output)
                return output
        except Exception as exc:
            raise PreviewError(f"Unable to generate image preview: {exc}", 400) from exc


def _save_png_atomic(image: object, destination: Path) -> None:
    fd, temporary_name = tempfile.mkstemp(prefix=f".{destination.stem}-", suffix=".png", dir=destination.parent)
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        image.save(temporary, "PNG")  # type: ignore[attr-defined]
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
