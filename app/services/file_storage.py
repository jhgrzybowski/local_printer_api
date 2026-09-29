from __future__ import annotations

import asyncio
import hashlib
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
from typing import Iterator

import fcntl

from starlette.datastructures import UploadFile

from app.services.mime_detection import SUPPORTED_MIME_TYPES, detect_mime
from app.services.office_conversion import ConversionError, OfficeConverter
from app.services.office_formats import OfficeFormatError, OFFICE_FORMATS, inspect_office
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
    converted: bool = False
    printable_sha256: str | None = None
    warnings: tuple[str, ...] = ()

    @property
    def printable_mime(self) -> str:
        return "application/pdf" if self.converted else self.detected_mime


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
        self.converted_dir = self.root / "converted"

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

        return await asyncio.to_thread(
            self._finish_upload, file_id, original_filename, sample, size_bytes, owner_user_id,
        )

    def _finish_upload(self, file_id: str, original_filename: str, sample: bytes,
                       size_bytes: int, owner_user_id: int) -> StoredFile:
        file_path = self.file_path(file_id)
        converted = False
        warnings: tuple[str, ...] = ()
        try:
            extension = inspect_office(file_path, original_filename)
            if extension:
                detected_mime = OFFICE_FORMATS[extension][0]
                page_count = OfficeConverter(self.root / "conversion-work").convert(
                    file_path, extension, self.converted_path(file_id),
                )
                converted = True
                warnings = ("Office layout may differ from Microsoft Office; review the generated PDF before printing.",)
                if extension in {"xlsx", "ods"}:
                    warnings += ("Spreadsheet pages follow saved print areas, paper sizes, and scaling; hidden sheets are not forced into the output.",)
            else:
                detected_mime = detect_mime(sample)
                if detected_mime not in SUPPORTED_MIME_TYPES:
                    raise StorageError(f"Unsupported MIME type: {detected_mime}", 415)
                page_count = self._page_count(file_path, detected_mime)
                if detected_mime in {"image/png", "image/jpeg"}:
                    from PIL import Image
                    try:
                        with Image.open(file_path) as image:
                            image.verify()
                    except Exception as exc:
                        raise StorageError("Corrupt or unreadable image", 400) from exc
            printable = self.converted_path(file_id) if converted else file_path
            with printable.open("rb") as source:
                digest = hashlib.file_digest(source, "sha256").hexdigest()
            record = StoredFile(
                file_id=file_id, original_filename=original_filename, detected_mime=detected_mime,
                size_bytes=size_bytes, page_count=page_count,
                preview_available=converted or detected_mime in {"application/pdf", "image/png", "image/jpeg"},
                owner_user_id=owner_user_id, converted=converted, printable_sha256=digest, warnings=warnings,
            )
            self.write_record(record)
            return record
        except Exception as exc:
            file_path.unlink(missing_ok=True)
            self.converted_path(file_id).unlink(missing_ok=True)
            self.metadata_path(file_id).unlink(missing_ok=True)
            if isinstance(exc, OfficeFormatError):
                raise StorageError(str(exc), 415) from exc
            if isinstance(exc, ConversionError):
                raise StorageError(exc.message, exc.status_code) from exc
            raise

    def converted_path(self, file_id: str) -> Path:
        return self.converted_dir / f"{file_id}.pdf"

    def printable_path(self, record: StoredFile) -> Path:
        return self.converted_path(record.file_id) if record.converted else self.file_path(record.file_id)

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
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.metadata_dir,
                                         prefix=f".{record.file_id}-", delete=False) as output:
            temporary = Path(output.name)
            try:
                output.write(json.dumps(asdict(record), sort_keys=True))
                output.flush()
                os.fsync(output.fileno())
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)

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
            try:
                with self.lease(file_id):
                    record = self.get_record(file_id)
                    if record is None or record.file_id != file_id:
                        raise LegacyClaimError("Upload metadata is invalid")
                    if record.owner_user_id is not None:
                        raise LegacyClaimError("Upload already has an owner")

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
            except StorageError as exc:
                raise LegacyClaimError("Upload or metadata not found") from exc

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
    def maintenance_lock(self, *, exclusive: bool) -> Iterator[None]:
        """Coordinate print submission with maintenance across API processes."""
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / ".maintenance.lock").open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

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

    def prune_expired(self, cutoff_timestamp: float, protected_file_ids: set[str]) -> int:
        """Remove expired upload groups, skipping files held by a worker."""
        removed = 0
        for metadata in self.metadata_dir.glob("*.json"):
            file_id = metadata.stem
            if not is_safe_file_id(file_id) or file_id in protected_file_ids:
                continue
            try:
                if metadata.stat().st_mtime >= cutoff_timestamp:
                    continue
                try:
                    source = self.file_path(file_id).open("rb")
                except FileNotFoundError:
                    # A lost payload cannot be leased or used for another
                    # print. Remove its old metadata and derived artifacts.
                    if (metadata.stat().st_mtime < cutoff_timestamp
                            and not self.file_path(file_id).exists()):
                        self._remove_upload_group(file_id, metadata)
                        removed += 1
                    continue
                with source:
                    try:
                        fcntl.flock(source, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        continue
                    # Recheck after taking the lock, since another worker may have touched it.
                    if metadata.stat().st_mtime >= cutoff_timestamp:
                        continue
                    self._remove_upload_group(file_id, metadata)
                    removed += 1
            except (FileNotFoundError, OSError):
                continue
        for artifact in self.converted_dir.glob("*.pdf"):
            if artifact.stem not in protected_file_ids and not self.metadata_path(artifact.stem).exists():
                try:
                    if artifact.stat().st_mtime < cutoff_timestamp:
                        artifact.unlink(missing_ok=True)
                except OSError:
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
                    if not self.metadata_path(file_id).exists():
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
                    if filtered.stat().st_mtime < cutoff_timestamp:
                        filtered.unlink()
            except (FileNotFoundError, OSError):
                continue
        work = self.root / "conversion-work"
        if work.is_dir():
            with (work / ".office.lock").open("a+b") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    pass
                else:
                    for abandoned in work.glob("office-*"):
                        try:
                            if abandoned.is_dir() and abandoned.stat().st_mtime < cutoff_timestamp:
                                shutil.rmtree(abandoned)
                        except OSError:
                            continue
        return removed

    def _remove_upload_group(self, file_id: str, metadata: Path) -> None:
        metadata.unlink(missing_ok=True)
        self.file_path(file_id).unlink(missing_ok=True)
        self.converted_path(file_id).unlink(missing_ok=True)
        shutil.rmtree(self.preview_dir(file_id), ignore_errors=True)
        for filtered in (
            self.filtered_dir / f"{file_id}.pdf",
            *self.filtered_dir.glob(f"{file_id}-*.pdf"),
        ):
            filtered.unlink(missing_ok=True)

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
    if len(name) > 180:
        suffix = Path(name).suffix
        if suffix and len(suffix) < 180:
            stem = name[:-len(suffix)]
            name = f"{stem[:180 - len(suffix)].rstrip('._-')}{suffix}"
        else:
            name = name[:180]
    name = name.strip("._-")
    return name or "upload"


def is_safe_file_id(file_id: str) -> bool:
    return bool(FILE_ID_RE.fullmatch(file_id))
