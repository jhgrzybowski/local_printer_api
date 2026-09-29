"""Recognize Office containers from content, before invoking a converter."""
from __future__ import annotations

from pathlib import Path, PurePosixPath
import re
from xml.etree import ElementTree as ET
import zlib
from zipfile import BadZipFile, ZipFile

OFFICE_FORMATS = {
    "docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", "word/document.xml", "writer_pdf_Export"),
    "xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "xl/workbook.xml", "calc_pdf_Export"),
    "pptx": ("application/vnd.openxmlformats-officedocument.presentationml.presentation", "ppt/presentation.xml", "impress_pdf_Export"),
    "odt": ("application/vnd.oasis.opendocument.text", "content.xml", "writer_pdf_Export"),
    "ods": ("application/vnd.oasis.opendocument.spreadsheet", "content.xml", "calc_pdf_Export"),
    "odp": ("application/vnd.oasis.opendocument.presentation", "content.xml", "impress_pdf_Export"),
}

# LibreOffice installs these document filters with their application component.
OFFICE_COMPONENTS = {
    "docx": "writer",
    "odt": "writer",
    "xlsx": "calc",
    "ods": "calc",
    "pptx": "impress",
    "odp": "impress",
}
MAX_EXPANDED_BYTES = 100 * 1024 * 1024
MAX_MEMBERS = 2000


class OfficeFormatError(ValueError):
    pass


def inspect_office(path: Path, filename: str) -> str | None:
    """Return a verified extension. Never trust the HTTP Content-Type header."""
    extension = Path(filename).suffix.lower().lstrip(".")
    if extension in {"doc", "xls", "ppt", "docm", "xlsm", "pptm", "rtf"}:
        raise OfficeFormatError("Legacy and macro-enabled Office formats are unsupported; save as DOCX, XLSX, PPTX, or PDF")
    with path.open("rb") as source:
        is_zip = source.read(4).startswith(b"PK")
    if extension not in OFFICE_FORMATS and not is_zip:
        return None
    if extension not in OFFICE_FORMATS:
        raise OfficeFormatError("Unsupported Office/archive type; use DOCX, XLSX, PPTX, ODT, ODS, or ODP")
    try:
        with ZipFile(path) as archive:
            entries = archive.infolist()
            names = {entry.filename for entry in entries}
            if len(entries) > MAX_MEMBERS or sum(e.file_size for e in entries) > MAX_EXPANDED_BYTES:
                raise OfficeFormatError("Office archive exceeds expanded size or entry limit")
            if len(names) != len(entries):
                raise OfficeFormatError("Office archive contains duplicate entries")
            mime, main_part, _ = OFFICE_FORMATS[extension]
            if main_part not in names:
                raise OfficeFormatError("Office content does not match its filename extension")
            if extension.startswith("od"):
                if "mimetype" not in names or archive.read("mimetype").decode("ascii") != mime:
                    raise OfficeFormatError("OpenDocument MIME type does not match its extension")
            elif "[Content_Types].xml" not in names:
                raise OfficeFormatError("Missing Office content types")
            for entry in entries:
                name = entry.filename
                parts = PurePosixPath(name).parts
                if name.startswith("/") or ".." in parts or "\\" in name or entry.flag_bits & 1:
                    raise OfficeFormatError("Unsafe or encrypted Office archive")
                lower = name.lower()
                if any(token in lower for token in ("vbaproject", "scripts/", "basic/", "embeddings/", "externallinks/", "activex/", "objectreplacements/")):
                    raise OfficeFormatError("Macros, embedded objects, and external data links are not supported")
                if extension.startswith("od"):
                    directory_parts = parts if entry.is_dir() else parts[:-1]
                    if any(part.lower().startswith("object") for part in directory_parts):
                        raise OfficeFormatError("Macros, embedded objects, and external data links are not supported")
                if lower.endswith((".xml", ".rels")):
                    data = archive.read(entry)
                    if b"<!DOCTYPE" in data.replace(b"\x00", b"").upper() or b"<!ENTITY" in data.replace(b"\x00", b"").upper():
                        raise OfficeFormatError("XML document types and entities are not supported")
                    root = ET.fromstring(data)
                    fields = "".join((node.text or "") for node in root.iter() if node.tag.endswith("}instrText"))
                    fields += " ".join(value for node in root.iter() for key, value in node.attrib.items() if key.endswith("}instr"))
                    if re.search(r"\b(?:DDEAUTO|DDE|INCLUDETEXT|INCLUDEPICTURE|DATABASE|LINK)\b", fields, re.IGNORECASE):
                        raise OfficeFormatError("Active or linked document fields are not supported")
                    for node in root.iter():
                        if node.tag.rsplit("}", 1)[-1].lower().startswith("dde"):
                            raise OfficeFormatError("DDE content is not supported")
                        if node.tag.endswith("encryption-data"):
                            raise OfficeFormatError("Encrypted Office documents are not supported")
                        if "macroenabled" in str(node.attrib).lower():
                            raise OfficeFormatError("Macro-enabled documents are not supported")
                        if node.attrib.get("TargetMode", "").lower() == "external" and not node.attrib.get("Type", "").endswith("/hyperlink"):
                            raise OfficeFormatError("Linked external content is not supported; embed it before uploading")
                        href = node.attrib.get("{http://www.w3.org/1999/xlink}href", "")
                        if href and not node.tag.endswith("}a") and (":" in href or href.startswith("/") or ".." in PurePosixPath(href).parts):
                            raise OfficeFormatError("Linked external content is not supported; embed it before uploading")
            return extension
    except OfficeFormatError:
        raise
    except (BadZipFile, KeyError, UnicodeError, ET.ParseError, RuntimeError, NotImplementedError, ValueError, zlib.error) as exc:
        raise OfficeFormatError("Corrupt or unreadable Office document") from exc
