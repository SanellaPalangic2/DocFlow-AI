"""The document pipeline in one place: validate -> extract -> analyze -> store.

Routes call `process_document`; it doesn't know about HTTP, so it is easy to test
and could be moved to a background worker later without changes.
"""
from flask import current_app

from . import db
from .analysis import run_analysis
from .extraction import extract_text
from .grounding import review_rows
from .validation import validate_upload


def process_document(filename: str, data: bytes, source: str = "upload", analyzer=None) -> int:
    cfg = current_app.config

    # 1. Validate (raises ValidationError with a user-safe message)
    f = validate_upload(filename, data, cfg["MAX_CONTENT_LENGTH"])

    # 2. Extract text (raises ExtractionError). The raw bytes are discarded after this.
    text, page_count = extract_text(f.file_type, f.data, cfg["MAX_PDF_PAGES"])

    # 3 + 4. LLM -> strict JSON -> normalized -> provenance-verified (raises AnalysisError)
    analysis, meta = run_analysis(text, cfg, analyzer=analyzer or current_app.extensions.get("analyzer"))

    # 5. Persist everything in one transaction
    return db.save_processed_document(
        meta={"original_name": f.safe_name, "file_type": f.file_type, "size_bytes": f.size_bytes,
              "sha256": f.sha256, "page_count": page_count, "source": source},
        text=text,
        analysis=analysis,
        provider=meta["provider"],
        model=meta["model"],
        truncated=meta["truncated"],
        review_rows=review_rows(analysis),
    )
