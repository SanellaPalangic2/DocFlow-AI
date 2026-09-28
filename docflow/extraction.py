"""Step 2 - Text extraction.

TXT: decode as UTF-8 (fallback cp1252 / latin-1 for older Windows exports).
PDF: pypdf text layer only. Scanned/image PDFs have no text layer; we detect that
and tell the user instead of sending garbage to the AI (OCR is a future step).
"""
import io
import logging
import re

from pypdf import PdfReader

log = logging.getLogger(__name__)


class ExtractionError(Exception):
    """A user-facing reason extraction failed."""


MIN_USEFUL_CHARS = 20


def _clean(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(ch for ch in text if ch == "\n" or ch == "\t" or ch.isprintable())
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_txt(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return _clean(data.decode(encoding))
        except UnicodeDecodeError:
            continue
    raise ExtractionError("Could not read this text file's encoding.")


def extract_pdf(data: bytes, max_pages: int) -> tuple[str, int]:
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise ExtractionError("This PDF is password-protected. Please upload an unlocked copy.")
        page_count = len(reader.pages)
        if page_count > max_pages:
            raise ExtractionError(f"This PDF has {page_count} pages (limit {max_pages}).")
        parts = []
        for i, page in enumerate(reader.pages):
            parts.append(page.extract_text() or "")
        return _clean("\n\n".join(parts)), page_count
    except ExtractionError:
        raise
    except Exception as exc:  # pypdf raises many types on malformed files
        log.warning("PDF extraction failed: %s", type(exc).__name__)
        raise ExtractionError("This PDF could not be read. It may be damaged or unusual.")


def extract_text(file_type: str, data: bytes, max_pdf_pages: int) -> tuple[str, int | None]:
    """Returns (text, page_count)."""
    if file_type == "txt":
        text, pages = extract_txt(data), None
    elif file_type == "pdf":
        text, pages = extract_pdf(data, max_pdf_pages)
    else:
        raise ExtractionError("Unsupported file type.")

    if len(text) < MIN_USEFUL_CHARS:
        if file_type == "pdf":
            raise ExtractionError(
                "No readable text was found. This looks like a scanned/image PDF, "
                "which needs OCR (not supported in this version)."
            )
        raise ExtractionError("The document contains too little text to analyze.")
    return text, pages
