"""The single source of truth for what an analysis looks like.

Every important extracted value carries its provenance:

    {
      "value":      "...",                             # or null
      "source":     "explicit" | "inferred" | "missing",
      "confidence": "high" | "medium" | "low" | null,  # null only when missing
      "evidence":   "exact excerpt from the document" | null,
    }

The LLM *proposes* these. `normalize_analysis` forces its output into exactly this
shape; `grounding.py` then re-derives source, confidence and evidence from the
document itself, so the final values are decided by code, not by the model.
"""

SOURCES = ("explicit", "inferred", "missing")
CONFIDENCES = ("high", "medium", "low")
CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}

# (key, label) - one value each
SINGLE_FIELDS = [
    ("document_type", "Document type"),
    ("customer_name", "Customer / client"),
    ("address", "Address"),
    ("email", "Email"),
    ("phone", "Phone"),
    ("requested_service", "Requested service"),
]

# (key, label) - lists of labeled values
LIST_FIELDS = [
    ("important_dates", "Important dates"),
    ("monetary_amounts", "Monetary amounts"),
]

# Identity/contact details must never be guessed. If the exact value is not in
# the document, it is discarded (-> MISSING) instead of being shown as INFERRED.
EXPLICIT_ONLY_FIELDS = {"customer_name", "address", "email", "phone"}

MAX_EVIDENCE_CHARS = 300

_FIELD_PROPS = {
    "value": {"type": ["string", "null"],
              "description": "Copy the value as written in the document. null if missing."},
    "source": {"type": "string", "enum": list(SOURCES)},
    "confidence": {"type": ["string", "null"], "enum": [*CONFIDENCES, None]},
    "evidence": {"type": ["string", "null"],
                 "description": "Shortest exact, contiguous excerpt copied character-for-character "
                                "from the document (max 150 characters, no ellipses). null if none."},
}
_FIELD = {"type": "object", "properties": _FIELD_PROPS,
          "required": ["value", "source", "confidence", "evidence"]}
_LABELED_FIELD = {"type": "object",
                  "properties": {"label": {"type": "string", "description": "e.g. 'Due date', 'Quote total'."},
                                 **_FIELD_PROPS},
                  "required": ["label", "value", "source", "confidence", "evidence"]}

# JSON Schema handed to the LLM as a forced tool call -> strict structured JSON.
ANALYSIS_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        **{key: _FIELD for key, _ in SINGLE_FIELDS},
        "important_dates": {"type": "array", "items": _LABELED_FIELD},
        "monetary_amounts": {"type": "array", "items": _LABELED_FIELD},
        "important_facts": {"type": "array", "items": _LABELED_FIELD,
                            "description": "Key facts staff must know. label = short topic."},
        "missing_information": {"type": "array", "items": {"type": "string"},
                                "description": "Information needed to act on this document that is absent."},
        "action_items": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string", "description": "2-3 sentence neutral summary."},
        "recommended_next_step": {"type": "string"},
    },
    "required": [key for key, _ in SINGLE_FIELDS] + [
        "important_dates", "monetary_amounts", "important_facts", "missing_information",
        "action_items", "summary", "recommended_next_step",
    ],
}

_LEGACY_SOURCE = {"found": "explicit"}  # accept the older 'status' vocabulary


def _str_or_none(v, max_len: int = 1000):
    if v is None or not isinstance(v, (str, int, float)) or isinstance(v, bool):
        return None
    s = str(v).strip()
    if s.lower() in {"", "null", "none", "n/a", "unknown", "not provided", "not stated"}:
        return None
    return s[:max_len]


def _field(raw) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    value = _str_or_none(raw.get("value"))
    source = str(raw.get("source") or raw.get("status") or "").lower()
    source = _LEGACY_SOURCE.get(source, source)
    if source not in SOURCES:
        source = "inferred"  # unknown label -> the weaker claim
    confidence = str(raw.get("confidence") or "").lower()
    confidence = confidence if confidence in CONFIDENCES else "low"
    evidence = _str_or_none(raw.get("evidence"), MAX_EVIDENCE_CHARS * 2)

    # The model said "missing" (or gave no value): nothing may be shown.
    # A value labeled missing is contradictory, so we drop it rather than guess.
    if value is None or source == "missing":
        return {"value": None, "source": "missing", "confidence": None, "evidence": None}
    return {"value": value, "source": source, "confidence": confidence, "evidence": evidence}


def _labeled_list(raw, limit: int = 20) -> list[dict]:
    items = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        f = _field(item)
        if f["source"] == "missing":
            continue  # an empty date/amount is noise, not information
        f["label"] = _str_or_none(item.get("label"), 80) or "Item"
        items.append(f)
    return items[:limit]


def _str_list(raw, limit: int = 20) -> list[str]:
    out = [_str_or_none(x, 400) for x in (raw if isinstance(raw, list) else [])]
    return [x for x in out if x][:limit]


def normalize_analysis(raw: dict) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    result = {key: _field(raw.get(key)) for key, _ in SINGLE_FIELDS}
    for key, _ in LIST_FIELDS:
        result[key] = _labeled_list(raw.get(key))
    result["important_facts"] = _labeled_list(raw.get("important_facts"))
    result["missing_information"] = _str_list(raw.get("missing_information"))
    result["action_items"] = _str_list(raw.get("action_items"))
    result["summary"] = _str_or_none(raw.get("summary"), 2000) or ""
    result["recommended_next_step"] = _str_or_none(raw.get("recommended_next_step"), 1000) or ""
    return result


def upgrade_legacy(analysis: dict) -> dict:
    """Display-compat for analyses stored by v1 ('status': found/inferred/missing)."""
    def fix(f):
        if isinstance(f, dict) and "source" not in f:
            f["source"] = _LEGACY_SOURCE.get(f.get("status"), f.get("status") or "missing")
            f.setdefault("confidence", None)
            f.setdefault("span", None)
            f.setdefault("reason", None)
        return f
    for key, _ in SINGLE_FIELDS:
        fix(analysis.get(key))
    for key in [k for k, _ in LIST_FIELDS] + ["important_facts"]:
        for item in analysis.get(key, []):
            fix(item)
    analysis.setdefault("discarded", [])
    st = analysis.get("stats", {})
    if "explicit" not in st:
        analysis["stats"] = {"explicit": st.get("found", 0), "inferred": st.get("inferred", 0),
                             "missing": st.get("missing", 0), "low_confidence": 0,
                             "corrected": st.get("unverified", 0)}
    return analysis
