"""HTTP layer only: parse the request, call the pipeline or DB, render a template."""
import json
import os

from flask import (Blueprint, abort, current_app, flash, jsonify, redirect, render_template,
                   request, url_for)

from . import db
from .analysis import AnalysisError
from .extraction import ExtractionError
from .pipeline import process_document
from .schema import upgrade_legacy
from .validation import ValidationError

bp = Blueprint("main", __name__)

DEMO_DIR = os.path.join(os.path.dirname(__file__), "demo_docs")

# Human-friendly descriptions for the demo gallery (all data is fictional).
DEMO_DESCRIPTIONS = {
    "hvac_service_request.txt": ("Service request", "AC repair email", "Customer asks for an urgent repair visit."),
    "roofing_estimate.pdf": ("Estimate · PDF", "Roof replacement quote", "Line items, total and a deposit."),
    "rental_application.txt": ("Application", "Rental application", "Tenant form with several gaps."),
    "insurance_claim_letter.txt": ("Claim letter", "Water-damage claim", "Insurer requests documents by a deadline."),
    "vague_inquiry.txt": ("Vague inquiry", "Two-line inquiry", "Missing almost every detail."),
}


def demo_files() -> list[dict]:
    if not current_app.config.get("DEMO_MODE") or not os.path.isdir(DEMO_DIR):
        return []
    out = []
    for name in sorted(os.listdir(DEMO_DIR)):
        if name.rsplit(".", 1)[-1] in ("txt", "pdf"):
            kind, title, desc = DEMO_DESCRIPTIONS.get(name, ("Sample", name, ""))
            out.append({"name": name, "kind": kind, "title": title, "desc": desc})
    return out


def _run(filename, data, source):
    try:
        doc_id = process_document(filename, data, source=source)
    except (ValidationError, ExtractionError, AnalysisError) as exc:
        flash(str(exc), "error")
        return redirect(url_for("main.index"))
    return redirect(url_for("main.document", doc_id=doc_id))


@bp.get("/healthz")
def healthz():
    """Used by Render to check the app is up. Reveals nothing about configuration."""
    return {"status": "ok"}


@bp.get("/")
def index():
    docs = []
    for row in db.list_documents():
        a = json.loads(row["analysis_json"]) if row["analysis_json"] else {}
        docs.append({**dict(row),
                     "doc_type": (a.get("document_type") or {}).get("value"),
                     "customer": (a.get("customer_name") or {}).get("value")})
    return render_template("index.html", docs=docs, demos=demo_files())


@bp.post("/upload")
def upload():
    file = request.files.get("document")
    if file is None or not file.filename:
        flash("Please choose a file to upload.", "error")
        return redirect(url_for("main.index"))
    # Read with a hard cap (Flask's MAX_CONTENT_LENGTH also enforces this).
    data = file.read(current_app.config["MAX_CONTENT_LENGTH"] + 1)
    return _run(file.filename, data, "upload")


@bp.post("/demo/<name>")
def demo(name):
    # Only names that actually exist in the demo folder are accepted (no path traversal).
    allowed = {d["name"] for d in demo_files()}
    if name not in allowed:
        abort(404)
    with open(os.path.join(DEMO_DIR, name), "rb") as fh:
        return _run(name, fh.read(), "demo")


def _verification_progress(reviews) -> dict:
    """Human verification state. Only a reviewer's approval makes a value verified."""
    with_value = [r for r in reviews if r["final_value"] or r["decision"] == "rejected"]
    return {
        "total": len(with_value),
        "verified": sum(1 for r in with_value if r["decision"] in ("approved", "edited")),
        "rejected": sum(1 for r in with_value if r["decision"] == "rejected"),
        "pending": sum(1 for r in with_value if r["decision"] == "pending"),
    }


@bp.get("/documents/<int:doc_id>")
def document(doc_id):
    doc, analysis_row, reviews = db.get_document(doc_id)
    if doc is None:
        abort(404)
    analysis = upgrade_legacy(json.loads(analysis_row["analysis_json"]))
    rows = [dict(r) for r in reviews]
    core_rows = [r for r in rows if "." not in r["field_key"]]
    list_rows = [r for r in rows if "." in r["field_key"]]
    facts = analysis.get("important_facts", [])
    needs_attention = sum(1 for r in rows if r["ai_source"] == "inferred"
                          or r["ai_confidence"] in ("low", "medium") or r["system_note"])
    needs_attention += sum(1 for f in facts if f["source"] == "inferred"
                           or f.get("confidence") in ("low", "medium") or f.get("note"))
    return render_template("document.html", doc=doc, a=analysis, meta=analysis_row,
                           core_rows=core_rows, list_rows=list_rows,
                           counts={"all": len(rows) + len(facts), "attention": needs_attention},
                           progress=_verification_progress(reviews))


@bp.post("/documents/<int:doc_id>/review")
def review(doc_id):
    doc, _, reviews = db.get_document(doc_id)
    if doc is None:
        abort(404)
    action = request.form.get("action", "save")
    if action not in ("save", "approve", "reject"):
        abort(400)

    decisions, undecided = {}, []
    for r in reviews:
        key = r["field_key"]
        value = (request.form.get(f"value__{key}", r["final_value"] or "") or "").strip()[:1000] or None
        choice = request.form.get(f"decision__{key}", "")
        if choice == "reject":
            decisions[key] = ("rejected", None)
        elif choice == "approve":
            decisions[key] = ("edited" if value != r["ai_value"] else "approved", value)
        else:
            decisions[key] = ("pending", value)
            if value:
                undecided.append(r["label"])

    note = (request.form.get("note") or "").strip()[:2000] or None

    if action == "approve" and undecided:
        db.save_review(doc_id, decisions, None, note)
        flash(f"Approve or reject every field before completing the review "
              f"({len(undecided)} still pending: {', '.join(undecided[:4])}{'…' if len(undecided) > 4 else ''}).",
              "error")
        return redirect(url_for("main.document", doc_id=doc_id))

    status = {"approve": "approved", "reject": "rejected"}.get(action)
    db.save_review(doc_id, decisions, status, note)
    flash({"approved": "Review complete - document approved.",
           "rejected": "Document marked as rejected.",
           None: "Progress saved."}[status], "success")
    return redirect(url_for("main.document", doc_id=doc_id))


@bp.get("/documents/<int:doc_id>/export.json")
def export(doc_id):
    """The record a downstream system (CRM, ERP) would receive.
    `verified_fields` contains ONLY values a human approved or corrected."""
    doc, analysis_row, reviews = db.get_document(doc_id)
    if doc is None:
        abort(404)
    analysis = upgrade_legacy(json.loads(analysis_row["analysis_json"]))
    fields = {}
    for r in reviews:
        verified = r["decision"] in ("approved", "edited")
        fields[r["field_key"]] = {
            "label": r["label"],
            "value": r["final_value"] if r["decision"] != "rejected" else None,
            "human_verified": verified,
            "decision": r["decision"],
            "ai_proposal": {
                "value": r["ai_value"],
                "source": (r["ai_source"] or "").upper(),
                "confidence": (r["ai_confidence"] or "").upper() or None,
                "evidence": r["evidence"],
                "evidence_offsets": [r["evidence_start"], r["evidence_end"]] if r["evidence"] else None,
                "reason": r["confidence_reason"],
                "system_correction": r["system_note"],
            },
        }
    return jsonify({
        "document_id": doc["id"],
        "file": doc["original_name"],
        "review_status": doc["review_status"],
        "reviewed_at": doc["reviewed_at"],
        "verified_fields": {k: v["value"] for k, v in fields.items() if v["human_verified"]},
        "fields": fields,
        "ai_generated_unverified": {
            "summary": analysis["summary"],
            "recommended_next_step": analysis["recommended_next_step"],
            "action_items": analysis["action_items"],
            "missing_information": analysis["missing_information"],
        },
        "analyzed_by": {"provider": analysis_row["provider"], "model": analysis_row["model"]},
    })
