from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from app.services.file_storage import StoredFile, TempFileStorage
from app.services.layout import DEFAULT_IMAGE_PPI
from app.settings import MAX_IMAGE_PIXELS, PREVIEW_CONCURRENCY, PREVIEW_DPI

PDF_PREVIEW_TIMEOUT_SECONDS = 30
PREVIEW_MAX_DIMENSION = 1600
# Long side of a thumbnail: enough for a page strip, about 1/70 of the bytes.
PREVIEW_THUMB_DIMENSION = 320
PREVIEW_SIZES = ("full", "thumb")
# zlib level 1 encodes rendered pages several times faster than the default 6;
# text pages come out about the same size and scans roughly 10% larger.
PNG_COMPRESS_LEVEL = 1

# A browser asks for many pages at once. Bound the renderers so a long
# document cannot start dozens of poppler processes on a small server.
_render_slots = threading.BoundedSemaphore(PREVIEW_CONCURRENCY)
_page_locks: dict[str, tuple[threading.Lock, int]] = {}
_page_locks_guard = threading.Lock()


class PreviewError(RuntimeError):
    def __init__(self, message: str, status_code: int = 500) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def render_pdf_page(source: Path, page: int, *, dpi: int, max_dimension: int, exact: bool) -> object:
    """Rasterize one page with a single poppler process.

    exact=True scales the long side to max_dimension (thumbnails). Otherwise
    the page renders at dpi and only pages larger than max_dimension shrink.
    """
    from PIL import Image

    sizing = ["-scale-to", str(max_dimension)] if exact else ["-r", str(dpi)]
    try:
        completed = subprocess.run(
            ["pdftoppm", "-f", str(page), "-l", str(page), *sizing, str(source)],
            capture_output=True, timeout=PDF_PREVIEW_TIMEOUT_SECONDS, check=False,
        )
    except FileNotFoundError as exc:
        raise PreviewError("Poppler is not installed or not available on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise PreviewError("PDF preview exceeded its time limit", 504) from exc
    if completed.returncode != 0 or not completed.stdout:
        raise PreviewError("Unable to render PDF preview page", 400)
    image = Image.open(io.BytesIO(completed.stdout))
    image.load()
    if not exact and max(image.size) > max_dimension:
        image.thumbnail((max_dimension, max_dimension))
    return image


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
            return [self._render_image(source, paths[0], PREVIEW_MAX_DIMENSION)]

        raise PreviewError("Preview is not available for this file type", 400)

    def preview_path(self, record: StoredFile, page: int, size: str = "full") -> Path:
        if page < 1:
            raise PreviewError("Invalid page number", 400)
        if record.page_count is not None and page > record.page_count:
            raise PreviewError("Invalid page number", 400)
        if size not in PREVIEW_SIZES:
            raise PreviewError("Invalid preview size", 400)

        self.ensure_previews(record)
        prefix = "page" if size == "full" else "thumb"
        path = self.storage.preview_dir(record.file_id) / f"{prefix}-{page}.png"
        if path.exists():
            return path
        source = self.storage.printable_path(record)
        dimension = PREVIEW_MAX_DIMENSION if size == "full" else PREVIEW_THUMB_DIMENSION
        with _page_lock(path):
            # Another request may have rendered this page while we waited.
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                with _render_slots:
                    if record.printable_mime == "application/pdf":
                        self._render_pdf(source, path, page, size)
                    elif record.detected_mime in {"image/png", "image/jpeg"}:
                        self._render_image(source, path, dimension)
        if not path.exists():
            raise PreviewError("Preview page not found", 404)
        return path

    def page_geometry(self, record: StoredFile) -> list[dict[str, float]] | None:
        """Displayed page sizes in points, cached beside the previews."""
        if record.printable_mime != "application/pdf" and record.detected_mime not in {"image/png", "image/jpeg"}:
            return None
        cache = self.storage.preview_dir(record.file_id) / "geometry.json"
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        source = self.storage.printable_path(record)
        if not source.exists():
            raise PreviewError("Stored file is missing", 404)
        try:
            if record.printable_mime == "application/pdf":
                geometry = _pdf_geometry(source)
            else:
                geometry = [_image_geometry(source)]
        except PreviewError:
            raise
        except Exception as exc:
            raise PreviewError(f"Unable to read page sizes: {exc}", 400) from exc
        cache.parent.mkdir(parents=True, exist_ok=True)
        _write_atomic(cache, json.dumps(geometry).encode())
        return geometry

    def _render_pdf(self, source: Path, destination: Path, page: int, size: str) -> None:
        exact = size == "thumb"
        image = render_pdf_page(
            source, page, dpi=self.dpi,
            max_dimension=PREVIEW_THUMB_DIMENSION if exact else PREVIEW_MAX_DIMENSION,
            exact=exact,
        )
        try:
            _save_png_atomic(image, destination)
        except Exception as exc:
            raise PreviewError(f"Unable to generate PDF preview: {exc}") from exc
        finally:
            image.close()  # type: ignore[attr-defined]

    def _render_image(self, source: Path, output: Path, dimension: int) -> Path:
        try:
            from PIL import Image
        except ImportError as exc:
            raise PreviewError("Pillow is not installed") from exc

        try:
            with Image.open(source) as image:
                if image.width * image.height > MAX_IMAGE_PIXELS:
                    raise PreviewError(
                        f"Image exceeds MAX_IMAGE_PIXELS={MAX_IMAGE_PIXELS}",
                        413,
                    )
                # JPEG can decode straight to a smaller scale, which skips most
                # of the work for camera photos.
                image.draft("RGB", (dimension, dimension))
                image.thumbnail((dimension, dimension))
                output.parent.mkdir(parents=True, exist_ok=True)
                _save_png_atomic(image.convert("RGB"), output)
                return output
        except PreviewError:
            raise
        except Exception as exc:
            raise PreviewError(f"Unable to generate image preview: {exc}", 400) from exc


@contextmanager
def _page_lock(path: Path) -> Iterator[None]:
    """Let one request render a page while others for it wait and reuse it."""
    key = str(path)
    with _page_locks_guard:
        lock, users = _page_locks.get(key, (threading.Lock(), 0))
        _page_locks[key] = (lock, users + 1)
    try:
        with lock:
            yield
    finally:
        with _page_locks_guard:
            lock, users = _page_locks[key]
            if users == 1:
                del _page_locks[key]
            else:
                _page_locks[key] = (lock, users - 1)


def _pdf_geometry(source: Path) -> list[dict[str, float]]:
    from pypdf import PdfReader

    geometry = []
    for page in PdfReader(source).pages:
        box = page.mediabox
        width, height = abs(float(box.width)), abs(float(box.height))
        if page.rotation % 180 == 90:
            width, height = height, width
        geometry.append({"width_pt": round(width, 2), "height_pt": round(height, 2)})
    return geometry


def _image_geometry(source: Path) -> dict[str, float]:
    from PIL import Image

    with Image.open(source) as image:
        dpi = image.info.get("dpi")
        try:
            x_dpi, y_dpi = (float(dpi[0]), float(dpi[1])) if dpi else (0.0, 0.0)
        except (TypeError, ValueError, IndexError):
            x_dpi = y_dpi = 0.0
        x_dpi = x_dpi if x_dpi > 1 else DEFAULT_IMAGE_PPI
        y_dpi = y_dpi if y_dpi > 1 else DEFAULT_IMAGE_PPI
        return {
            "width_pt": round(image.width * 72 / x_dpi, 2),
            "height_pt": round(image.height * 72 / y_dpi, 2),
        }


def _write_atomic(destination: Path, data: bytes) -> None:
    fd, temporary_name = tempfile.mkstemp(prefix=f".{destination.stem}-", dir=destination.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(data)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def _save_png_atomic(image: object, destination: Path) -> None:
    fd, temporary_name = tempfile.mkstemp(prefix=f".{destination.stem}-", suffix=".png", dir=destination.parent)
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        image.save(temporary, "PNG", compress_level=PNG_COMPRESS_LEVEL)  # type: ignore[attr-defined]
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
