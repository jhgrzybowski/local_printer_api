from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Callable, Iterator

import fcntl

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


class LegacyClaimError(ValueError):
    """A local operator cannot claim the requested legacy upload."""


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

    async def save_upload(self, upload: UploadFile, owner_user_id: int) -> StoredFile:
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

    def list_unowned_records(self) -> list[StoredFile]:
        """Inventory readable legacy metadata with a matching uploaded file."""
        if not self.metadata_dir.is_dir():
            return []
        records = []
        for path in sorted(self.metadata_dir.glob("*.json")):
            record = self.get_record(path.stem)
            if (
                record is not None
                and record.file_id == path.stem
                and record.owner_user_id is None
                and self.file_path(record.file_id).is_file()
            ):
                records.append(record)
        return records

    def claim_legacy_file(self, file_id: str, owner_user_id: int) -> StoredFile:
        """Assign one pre-account upload; never transfer an already owned file."""
        if not is_safe_file_id(file_id) or owner_user_id < 1:
            raise LegacyClaimError("Invalid file ID or owner user ID")
        path = self.metadata_path(file_id)
        if not path.is_file() or not self.file_path(file_id).is_file():
            raise LegacyClaimError("Upload or metadata not found")

        # A separate lock file survives os.replace, so two operator invocations
        # cannot both claim the same original inode for different users.
        with (self.metadata_dir / f"{file_id}.claim.lock").open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            record = self.get_record(file_id)
            if record is None or record.file_id != file_id:
                raise LegacyClaimError("Upload metadata is invalid")
            if record.owner_user_id is not None:
                raise LegacyClaimError("Upload already has an owner")
            if not self.file_path(file_id).is_file():
                raise LegacyClaimError("Uploaded file not found")

            claimed = replace(record, owner_user_id=owner_user_id)
            temporary_path: Path | None = None
            try:
                original_stat = path.stat()
                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=self.metadata_dir,
                    prefix=f".{file_id}.", suffix=".tmp", delete=False,
                ) as temporary:
                    temporary_path = Path(temporary.name)
                    temporary.write(json.dumps(asdict(claimed), sort_keys=True))
                    temporary.flush()
                    os.fchmod(temporary.fileno(), stat.S_IMODE(original_stat.st_mode))
                    os.fchown(temporary.fileno(), original_stat.st_uid, original_stat.st_gid)
                    os.fsync(temporary.fileno())
                os.replace(temporary_path, path)
            finally:
                if temporary_path is not None:
                    temporary_path.unlink(missing_ok=True)
            return claimed

    def file_path(self, file_id: str) -> Path:
        return self.files_dir / file_id

    def preview_dir(self, file_id: str) -> Path:
        return self.previews_dir / file_id

    def metadata_path(self, file_id: str) -> Path:
        return self.metadata_dir / f"{file_id}.json"

    def filtered_pdf_path(self, file_id: str) -> Path:
        self.filtered_dir.mkdir(parents=True, exist_ok=True)
        return self.filtered_dir / f"{file_id}-{secrets.token_urlsafe(12)}.pdf"

    @contextmanager
    def lease(self, file_id: str) -> Iterator[None]:
        """Keep cleanup from removing an upload during rendering or spooling."""
        if not is_safe_file_id(file_id):
            raise StorageError("Stored file is missing", 404)
        try:
            source = self.file_path(file_id).open("rb")
        except FileNotFoundError as exc:
            raise StorageError("Stored file is missing", 404) from exc
        with source:
            fcntl.flock(source, fcntl.LOCK_SH)
            try:
                # Cleanup can unlink the pathname while this open descriptor
                # waits for its lock. Only lease the inode still in storage.
                try:
                    current = self.file_path(file_id).stat()
                except FileNotFoundError as exc:
                    raise StorageError("Stored file is missing", 404) from exc
                opened = os.fstat(source.fileno())
                if ((opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
                        or not self.metadata_path(file_id).exists()):
                    raise StorageError("Stored file is missing", 404)
                yield
            finally:
                fcntl.flock(source, fcntl.LOCK_UN)

    def prune_expired(
        self, cutoff_timestamp: float, protected_file_ids: set[str],
        is_protected: Callable[[str], bool] | None = None,
    ) -> int:
        """Remove expired upload groups, skipping files held by a worker."""
        removed = 0
        for metadata in self.metadata_dir.glob("*.json"):
            file_id = metadata.stem
            if not is_safe_file_id(file_id) or file_id in protected_file_ids:
                continue
            try:
                if metadata.stat().st_mtime >= cutoff_timestamp:
                    continue
                with self.file_path(file_id).open("rb") as source:
                    try:
                        fcntl.flock(source, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        continue
                    # Recheck after taking the lock, since another worker may have touched it.
                    if metadata.stat().st_mtime >= cutoff_timestamp:
                        continue
                    if is_protected is not None and is_protected(file_id):
                        continue
                    metadata.unlink(missing_ok=True)
                    self.file_path(file_id).unlink(missing_ok=True)
                    shutil.rmtree(self.preview_dir(file_id), ignore_errors=True)
                    for filtered in (
                        self.filtered_dir / f"{file_id}.pdf",
                        *self.filtered_dir.glob(f"{file_id}-*.pdf"),
                    ):
                        filtered.unlink(missing_ok=True)
                    removed += 1
            except (FileNotFoundError, OSError):
                continue
        # A crash can leave a payload or derived output without metadata.
        for source_path in self.files_dir.glob("*"):
            file_id = source_path.name
            if (not is_safe_file_id(file_id) or file_id in protected_file_ids
                    or self.metadata_path(file_id).exists()):
                continue
            try:
                if source_path.stat().st_mtime >= cutoff_timestamp:
                    continue
                with source_path.open("rb") as source:
                    try:
                        fcntl.flock(source, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        continue
                    if (not self.metadata_path(file_id).exists()
                            and (is_protected is None or not is_protected(file_id))):
                        source_path.unlink(missing_ok=True)
                        removed += 1
            except (FileNotFoundError, OSError):
                continue
        for preview_dir in self.previews_dir.glob("*"):
            file_id = preview_dir.name
            if (not is_safe_file_id(file_id) or file_id in protected_file_ids
                    or self.metadata_path(file_id).exists()):
                continue
            try:
                if preview_dir.stat().st_mtime < cutoff_timestamp:
                    shutil.rmtree(preview_dir)
            except (FileNotFoundError, OSError):
                continue
        source_ids = sorted(
            (path.name for path in self.files_dir.glob("*") if is_safe_file_id(path.name)),
            key=len, reverse=True,
        )
        for filtered in self.filtered_dir.glob("*.pdf"):
            file_id = next(
                (candidate for candidate in source_ids
                 if filtered.name == f"{candidate}.pdf"
                 or filtered.name.startswith(f"{candidate}-")),
                None,
            )
            if file_id in protected_file_ids:
                continue
            try:
                if file_id is None:
                    if filtered.stat().st_mtime < cutoff_timestamp:
                        filtered.unlink()
                    continue
                with self.file_path(file_id).open("rb") as source:
                    try:
                        fcntl.flock(source, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        continue
                    # A page-range print holds the source lease while writing
                    # this PDF. Check its age and active-job mapping under lock.
                    if (filtered.stat().st_mtime < cutoff_timestamp
                            and (is_protected is None or not is_protected(file_id))):
                        filtered.unlink()
            except (FileNotFoundError, OSError):
                continue
        return removed

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
