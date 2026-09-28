"""SQLite persistence. Plain sqlite3 - no ORM - so every query is visible.

Tables
------
documents      one row per uploaded document (metadata + extracted text + review status)
analyses       the AI output for a document (raw JSON, provider, model)
field_reviews  one row per reviewable field: what the AI said vs. what the human decided
"""
import json
import os
import sqlite3
from datetime import datetime, timezone

from flask import current_app, g

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    original_name   TEXT NOT NULL,          -- sanitized filename, display only
    file_type       TEXT NOT NULL,          -- 'pdf' | 'txt'
    size_bytes      INTEGER NOT NULL,
    sha256          TEXT NOT NULL,
    page_count      INTEGER,
    char_count      INTEGER NOT NULL,
    extracted_text  TEXT NOT NULL,
    source          TEXT NOT NULL DEFAULT 'upload',   -- 'upload' | 'demo'
    review_status   TEXT NOT NULL DEFAULT 'pending_review',
                    -- pending_review | approved | rejected
    reviewer_note   TEXT,
    created_at      TEXT NOT NULL,
    reviewed_at     TEXT
);

CREATE TABLE IF NOT EXISTS analyses (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id     INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    provider        TEXT NOT NULL,          -- 'anthropic' | 'offline-rules'
    model           TEXT,
    analysis_json   TEXT NOT NULL,          -- validated + grounded analysis
    truncated       INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS field_reviews (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id       INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    field_key         TEXT NOT NULL,        -- e.g. 'email', 'important_dates.0'
    label             TEXT NOT NULL,
    -- what the AI proposed, after code verification (never overwritten)
    ai_value          TEXT,
    ai_source         TEXT NOT NULL,        -- explicit | inferred | missing
    ai_confidence     TEXT,                 -- high | medium | low | NULL when missing
    evidence          TEXT,                 -- exact slice of extracted_text, or NULL
    evidence_start    INTEGER,              -- char offsets into documents.extracted_text
    evidence_end      INTEGER,
    confidence_reason TEXT,                 -- why this source/confidence was assigned
    system_note       TEXT,                 -- set when code corrected the AI's claim
    -- what the human decided. Only approved/edited values count as verified.
    final_value       TEXT,
    decision          TEXT NOT NULL DEFAULT 'pending',
                      -- pending | approved | edited | rejected
    updated_at        TEXT NOT NULL,
    UNIQUE(document_id, field_key)
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        path = current_app.config["DATABASE_PATH"]
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        g.db = conn
    return g.db


def close_db(_exc=None) -> None:
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


SCHEMA_VERSION = 2


def _migrate(conn: sqlite3.Connection) -> None:
    """Upgrade databases created by v1 (which had a single 'ai_status' column)."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(field_reviews)")}
    if "ai_status" in cols:
        conn.execute("ALTER TABLE field_reviews RENAME COLUMN ai_status TO ai_source")
        conn.execute("UPDATE field_reviews SET ai_source = 'explicit' WHERE ai_source = 'found'")
        for col in ("ai_confidence TEXT", "evidence TEXT", "evidence_start INTEGER",
                    "evidence_end INTEGER", "confidence_reason TEXT", "system_note TEXT"):
            conn.execute(f"ALTER TABLE field_reviews ADD COLUMN {col}")
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def init_db(app) -> None:
    with app.app_context():
        conn = get_db()
        conn.executescript(SCHEMA)
        if conn.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
            _migrate(conn)
        conn.commit()
    app.teardown_appcontext(close_db)


# ---------------------------------------------------------------- writes

def save_processed_document(meta: dict, text: str, analysis: dict, provider: str,
                            model: str | None, truncated: bool, review_rows: list[dict]) -> int:
    """Store document, analysis and initial review rows in ONE transaction,
    so we never end up with a document that has no analysis."""
    db = get_db()
    ts = now()
    with db:  # commits on success, rolls back on exception
        cur = db.execute(
            """INSERT INTO documents (original_name, file_type, size_bytes, sha256, page_count,
                                      char_count, extracted_text, source, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (meta["original_name"], meta["file_type"], meta["size_bytes"], meta["sha256"],
             meta.get("page_count"), len(text), text, meta.get("source", "upload"), ts),
        )
        doc_id = cur.lastrowid
        db.execute(
            """INSERT INTO analyses (document_id, provider, model, analysis_json, truncated, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (doc_id, provider, model, json.dumps(analysis), int(truncated), ts),
        )
        db.executemany(
            """INSERT INTO field_reviews (document_id, field_key, label, ai_value, ai_source,
                   ai_confidence, evidence, evidence_start, evidence_end, confidence_reason,
                   system_note, final_value, decision, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
            [(doc_id, r["key"], r["label"], r["value"], r["source"], r["confidence"],
              r["evidence"], (r["span"] or [None])[0], (r["span"] or [None, None])[1],
              r["reason"], r["note"], r["value"], ts)
             for r in review_rows],
        )
    return doc_id


def save_review(doc_id: int, decisions: dict[str, tuple[str, str | None]],
                review_status: str | None, note: str | None) -> None:
    """decisions: field_key -> (decision, final_value)."""
    db = get_db()
    ts = now()
    with db:
        for key, (decision, final_value) in decisions.items():
            db.execute(
                """UPDATE field_reviews SET decision = ?, final_value = ?, updated_at = ?
                   WHERE document_id = ? AND field_key = ?""",
                (decision, final_value, ts, doc_id, key),
            )
        if review_status:
            db.execute(
                "UPDATE documents SET review_status = ?, reviewer_note = ?, reviewed_at = ? WHERE id = ?",
                (review_status, note, ts, doc_id),
            )
        else:
            db.execute("UPDATE documents SET reviewer_note = ? WHERE id = ?", (note, doc_id))


# ---------------------------------------------------------------- reads

def list_documents(limit: int = 25):
    return get_db().execute(
        """SELECT d.id, d.original_name, d.file_type, d.source, d.review_status, d.created_at,
                  a.analysis_json
           FROM documents d LEFT JOIN analyses a ON a.document_id = d.id
           ORDER BY d.id DESC LIMIT ?""",
        (limit,),
    ).fetchall()


def get_document(doc_id: int):
    doc = get_db().execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
    if doc is None:
        return None, None, []
    analysis = get_db().execute(
        "SELECT * FROM analyses WHERE document_id = ? ORDER BY id DESC LIMIT 1", (doc_id,)
    ).fetchone()
    reviews = get_db().execute(
        "SELECT * FROM field_reviews WHERE document_id = ? ORDER BY id", (doc_id,)
    ).fetchall()
    return doc, analysis, reviews
