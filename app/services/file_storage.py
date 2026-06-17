from __future__ import annotations

import json
import os
import re
import secrets
from dataclasses import asdict, dataclass
from pathlib import Path

from starlette.datastructures import UploadFile

from app.services.mime_detection import SUPPORTED_MIME_TYPES, detect_mime
from app.services.pdf_metadata import PdfMetadataError, get_pdf_page_count
from app.settings import MAX_UPLOAD_MB, TMP_DIR


FILE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,80}$")
CHUNK_SIZE = 1024 * 1024


class StorageError(ValueError):
    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class StoredFile:
    file_id: str
    original_filename: str
    detected_mime: str
    size_bytes: int
    page_count: int | None
    preview_available: bool
    owner_user_id: int | None = None


class TempFileStorage:
    def __init__(
        self,
        tmp_dir: str | os.PathLike[str] | None = None,
        max_upload_mb: int | None = None,
    ) -> None:
        self.root = Path(tmp_dir or os.getenv("TMP_DIR", TMP_DIR))
        self.max_upload_mb = int(os.getenv("MAX_UPLOAD_MB", str(max_upload_mb or MAX_UPLOAD_MB)))
        self.files_dir = self.root / "files"
        self.metadata_dir = self.root / "metadata"
        self.previews_dir = self.root / "previews"
        self.filtered_dir = self.root / "filtered"

    async def save_upload(self, upload: UploadFile, owner_user_id: int | None = None) -> StoredFile:
        self._ensure_dirs()
        file_id = self._new_file_id()
        original_filename = sanitize_filename(upload.filename)
        file_path = self.file_path(file_id)

        sample = b""
        size_bytes = 0
        max_bytes = self.max_upload_mb * 1024 * 1024

        try:
            with file_path.open("xb") as destination:
                while True:
                    chunk = await upload.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    if not sample:
                        sample = chunk[:8192]
                    size_bytes += len(chunk)
                    if size_bytes > max_bytes:
                        raise StorageError(
                            f"Upload exceeds MAX_UPLOAD_MB={self.max_upload_mb}",
                            413,
                        )
                    destination.write(chunk)
        except Exception:
            file_path.unlink(missing_ok=True)
            raise
        finally:
            await upload.close()

        if size_bytes == 0:
            file_path.unlink(missing_ok=True)
            raise StorageError("Uploaded file is empty", 400)

        detected_mime = detect_mime(sample)
        if detected_mime not in SUPPORTED_MIME_TYPES:
            file_path.unlink(missing_ok=True)
            raise StorageError(f"Unsupported MIME type: {detected_mime}", 415)

        page_count = self._page_count(file_path, detected_mime)
        preview_available = detected_mime in {"application/pdf", "image/png", "image/jpeg"}

        record = StoredFile(
            file_id=file_id,
            original_filename=original_filename,
            detected_mime=detected_mime,
            size_bytes=size_bytes,
            page_count=page_count,
            preview_available=preview_available,
            owner_user_id=owner_user_id,
        )
        self.write_record(record)
        return record

    def get_record(self, file_id: str) -> StoredFile | None:
        if not is_safe_file_id(file_id):
            return None

        path = self.metadata_path(file_id)
        if not path.exists():
            return None

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return StoredFile(**data)
        except (OSError, TypeError, ValueError):
            return None

    def write_record(self, record: StoredFile) -> None:
        self._ensure_dirs()
        path = self.metadata_path(record.file_id)
        path.write_text(json.dumps(asdict(record), sort_keys=True), encoding="utf-8")

    def file_path(self, file_id: str) -> Path:
        return self.files_dir / file_id

    def preview_dir(self, file_id: str) -> Path:
        return self.previews_dir / file_id

    def metadata_path(self, file_id: str) -> Path:
        return self.metadata_dir / f"{file_id}.json"

    def filtered_pdf_path(self, file_id: str) -> Path:
        self.filtered_dir.mkdir(parents=True, exist_ok=True)
        return self.filtered_dir / f"{file_id}.pdf"

    def _ensure_dirs(self) -> None:
        self.files_dir.mkdir(parents=True, exist_ok=True)
        self.metadata_dir.mkdir(parents=True, exist_ok=True)
        self.previews_dir.mkdir(parents=True, exist_ok=True)
        self.filtered_dir.mkdir(parents=True, exist_ok=True)

    def _new_file_id(self) -> str:
        while True:
            file_id = secrets.token_urlsafe(18)
            if not self.metadata_path(file_id).exists() and not self.file_path(file_id).exists():
                return file_id

    def _page_count(self, file_path: Path, detected_mime: str) -> int | None:
        if detected_mime == "application/pdf":
            try:
                return get_pdf_page_count(file_path)
            except PdfMetadataError as exc:
                file_path.unlink(missing_ok=True)
                raise StorageError(exc.message, 400) from exc
        if detected_mime in {"image/png", "image/jpeg"}:
            return 1
        return None


def sanitize_filename(filename: str | None) -> str:
    name = (filename or "upload").replace("\\", "/").rsplit("/", 1)[-1].strip()
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name)
    name = name.lstrip(".")
    name = name[:180].strip("._-")
    return name or "upload"


def is_safe_file_id(file_id: str) -> bool:
    return bool(FILE_ID_RE.fullmatch(file_id))
