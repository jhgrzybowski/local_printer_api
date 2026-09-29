from __future__ import annotations

from pathlib import Path

from pypdf import PdfReader
from pypdf.errors import PdfReadError


class PdfMetadataError(ValueError):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def get_pdf_page_count(path: Path) -> int:
    try:
        reader = PdfReader(path)
    except PdfReadError as exc:
        raise PdfMetadataError("Corrupt or unreadable PDF") from exc
    except Exception as exc:
        raise PdfMetadataError("Unable to read PDF metadata") from exc

    if reader.is_encrypted:
        raise PdfMetadataError("Password-protected PDFs are not supported")

    try:
        page_count = len(reader.pages)
        if page_count < 1:
            raise PdfMetadataError("PDF must contain at least one page")
        return page_count
    except PdfMetadataError:
        raise
    except PdfReadError as exc:
        raise PdfMetadataError("Corrupt or unreadable PDF") from exc
    except Exception as exc:
        raise PdfMetadataError("Unable to read PDF page count") from exc
