"""Provenance verification - the app's anti-hallucination layer.

The LLM *proposes* value / source / confidence / evidence. This module decides the
final answer using only the document text:

1. Evidence is never taken from the model. We search for the model's excerpt in the
   document and, if it's there, store the *document's own characters* at that
   position (plus start/end offsets). If it isn't there, there is no evidence.
   So stored evidence is always a real slice of the uploaded document.

2. Source is checked:
     EXPLICIT  needs the value itself to be in the document (in the excerpt, or
               located by us elsewhere in the text). Otherwise -> INFERRED.
     INFERRED  stays inferred (never silently upgraded) and must have a real excerpt
               from the document as its basis.
     MISSING   anything with no support in the document. A value whose excerpt AND
               value are both absent is discarded, as is any identity/contact value
               that isn't literally written. Discarded proposals stay listed for the
               reviewer ("Discarded by DocFlow") but are never shown as data.

3. Confidence = the model's self-rating, *capped* by what code could verify:
     explicit + exact excerpt containing the value ........ up to HIGH
     explicit but excerpt had to be located by the system .. max MEDIUM
     inferred (always has a matched excerpt) ................ max MEDIUM
     several different emails/phones in the document ........ max MEDIUM
   LOW therefore comes from the model rating its own inference as weak.

Nothing here marks data as *verified* - only a human reviewer can do that.
"""
import re
import unicodedata

from .schema import (CONFIDENCE_RANK, EXPLICIT_ONLY_FIELDS, LIST_FIELDS, MAX_EVIDENCE_CHARS,
                     SINGLE_FIELDS)

_TRANSLATE = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"',
                            "–": "-", "—": "-", " ": " "})
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
PHONE_RE = re.compile(r"(?:\+?1[\s.-]?)?\(?\b\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}\b")


# ------------------------------------------------------------------ text matching

class DocIndex:
    """The document in 'loose' form (lowercase, punctuation/whitespace -> single space)
    with a map back to original character offsets, so a loose match can be turned
    into an exact slice of the original text."""

    def __init__(self, text: str):
        self.text = text
        chars, index = [" "], [0]
        for i, c in enumerate(text):
            for ch in unicodedata.normalize("NFKC", c).translate(_TRANSLATE).lower():
                if ch.isalnum():
                    chars.append(ch)
                    index.append(i)
                elif chars[-1] != " ":
                    chars.append(" ")
                    index.append(i)
        chars.append(" ")
        index.append(len(text))
        self.loose = "".join(chars)
        self.index = index
        self.digits = re.sub(r"\D", "", text)

    def find(self, needle: str | None) -> tuple[int, int] | None:
        """Return (start, end) in the ORIGINAL text of the first token-aligned match."""
        n = loose(needle)
        if len(n.strip()) < 2:
            return None
        pos = self.loose.find(n)  # n is padded with spaces -> whole-token match
        if pos == -1:
            return None
        start = self.index[pos + 1]
        end = self.index[pos + len(n) - 2] + 1
        return self._widen(start, end)

    def _widen(self, start: int, end: int) -> tuple[int, int]:
        """Loose matching skips punctuation, so re-attach symbols that belong to the
        match: '(312) 555-0101' not '312) 555-0101', '$1,240.00' not '1,240.00'."""
        t = self.text
        while start > 0 and t[start - 1] in "($#€£\"'“":
            start -= 1
        while end < len(t) and (t[end] in "%\"'”" or
                                (t[end] == ")" and t.count("(", start, end) > t.count(")", start, end))):
            end += 1
        return start, end


def loose(s: str | None) -> str:
    if not s:
        return " "
    s = unicodedata.normalize("NFKC", s).translate(_TRANSLATE).lower()
    return " " + " ".join(re.findall(r"[^\W_]+", s)) + " "


def _contains(haystack: str, needle: str) -> bool:
    n = loose(needle)
    return len(n.strip()) >= 1 and n in loose(haystack)


def _line_span(text: str, start: int, end: int) -> tuple[int, int]:
    """Expand a span to its full line(s), trimmed, and capped to MAX_EVIDENCE_CHARS."""
    s = text.rfind("\n", 0, start) + 1
    e = text.find("\n", end)
    e = len(text) if e == -1 else e
    while s < start and text[s].isspace():
        s += 1
    while e > end and text[e - 1].isspace():
        e -= 1
    if e - s > MAX_EVIDENCE_CHARS:  # long line: window around the value
        pad = max(0, (MAX_EVIDENCE_CHARS - (end - start)) // 2)
        s, e = max(s, start - pad), min(e, end + pad)
    return s, e


def _cap(confidence: str, ceiling: str) -> str:
    return confidence if CONFIDENCE_RANK[confidence] <= CONFIDENCE_RANK[ceiling] else ceiling


def _locate_evidence(idx: DocIndex, evidence: str | None, value: str | None) -> tuple[int, int] | None:
    """Find the model's excerpt in the document. If it contains '...', try each piece,
    preferring pieces that contain the value, then longer pieces."""
    if not evidence:
        return None
    pieces = [p for p in re.split(r"\.{3}|…", evidence) if len(p.strip()) >= 8]
    candidates = [evidence] + sorted(pieces, key=lambda p: (not _contains(p, value or ""), -len(p)))
    for cand in candidates:
        span = idx.find(cand)
        if span:
            s, e = span
            if e - s > MAX_EVIDENCE_CHARS:
                e = s + MAX_EVIDENCE_CHARS
            return s, e
    return None


def _locate_value(idx: DocIndex, key: str, value: str) -> tuple[int, int] | None:
    if key == "phone":
        d = re.sub(r"\D", "", value)[-10:]
        if len(d) < 7:
            return None
        for m in PHONE_RE.finditer(idx.text):
            if re.sub(r"\D", "", m.group(0)).endswith(d):
                return m.start(), m.end()
        return None
    if len(loose(value).strip()) < 4:  # too short to locate safely ("May", "$89")
        return None
    return idx.find(value)


# ------------------------------------------------------------------ verification

def _result(field, source, confidence, span, idx, reason, note=None):
    out = dict(field)
    out.update(source=source, confidence=confidence, reason=reason, note=note,
               span=list(span) if span else None,
               evidence=idx.text[span[0]:span[1]] if span else None)
    return out


def _discard(field, idx, note):
    """No support in the document -> MISSING. The note keeps the proposal visible for review."""
    return _result({**field, "value": None}, "missing", None, None, idx, "Not stated in the document.", note)


def verify_field(field: dict, key: str, idx: DocIndex) -> dict:
    if field["source"] == "missing":
        return _result(field, "missing", None, None, idx, "Not stated in the document.")

    value, claimed, model_conf = field["value"], field["source"], field["confidence"]
    ev_span = _locate_evidence(idx, field["evidence"], field["value"])
    excerpt = idx.text[ev_span[0]:ev_span[1]] if ev_span else ""
    value_in_excerpt = bool(ev_span) and (
        _contains(excerpt, value) or
        (key == "phone" and re.sub(r"\D", "", value)[-10:] in re.sub(r"\D", "", excerpt)))
    note = None

    if claimed == "explicit":
        if value_in_excerpt:
            source, conf, span = "explicit", model_conf, ev_span
            reason = "Value appears in the quoted excerpt."
        elif val_span := _locate_value(idx, key, value):
            source, conf, span = "explicit", _cap(model_conf, "medium"), _line_span(idx.text, *val_span)
            reason = "Value appears in the document; the excerpt shown was located by the system."
            if not ev_span:
                note = "The AI's quoted excerpt was not in the document. DocFlow found the value itself."
        elif ev_span:
            source, conf, span = "inferred", _cap(model_conf, "medium"), ev_span
            reason = "The exact value is not written in the document; it is reworded from the excerpt."
            note = "The AI labeled this as stated in the document, but the exact wording isn't there. Relabeled as inferred."
        else:  # neither the value nor the excerpt is in the document
            return _discard(field, idx, (
                f"The AI claimed the document says “{(field['evidence'] or '')[:100]}”, but it does "
                "not, and the value itself isn't in the document. Discarded." if field["evidence"]
                else "The AI gave no excerpt and the value isn't in the document. Discarded."))
    else:  # inferred: never upgraded automatically, and must have a real basis
        if ev_span:
            source, conf, span = "inferred", _cap(model_conf, "medium"), ev_span
            reason = "Not stated directly; inferred from the excerpt shown."
        else:  # an inference with nothing in the document to base it on is a guess
            return _discard(field, idx, (
                f"The AI inferred “{value[:80]}”, but its supporting excerpt is not in the document. Discarded."
                if field["evidence"] else
                f"The AI inferred “{value[:80]}” without any supporting text. Discarded."))

    # Any value containing an email/phone that isn't in the document is fabricated
    # contact data - discard it wherever it appears (facts, lists, any field).
    invented = [m.group(0) for m in EMAIL_RE.finditer(value) if not idx.find(m.group(0))]
    invented += [m.group(0) for m in PHONE_RE.finditer(value) if not _locate_value(idx, "phone", m.group(0))]
    if invented:
        return _discard(field, idx, f"The AI suggested “{value[:80]}”, but {invented[0]} does not "
                                    "appear in the document. Discarded. Contact details are never guessed.")

    # Identity / contact details: explicit or nothing.
    if key in EXPLICIT_ONLY_FIELDS and source != "explicit":
        return _discard(field, idx, f"The AI suggested “{value[:80]}”, but it is not written in the "
                                    "document. Discarded. Identity and contact details are never guessed.")

    # Ambiguity: several different emails/phones -> could be the wrong party's.
    if source == "explicit" and key in ("email", "phone"):
        pattern = EMAIL_RE if key == "email" else PHONE_RE
        distinct = {re.sub(r"\W", "", m.group(0).lower()) for m in pattern.finditer(idx.text)}
        if len(distinct) > 1:
            conf = _cap(conf, "medium")
            reason += f" The document contains {len(distinct)} different {key}s. Confirm this one is the customer's."

    return _result(field, source, conf, span, idx, reason, note)


def ground_analysis(analysis: dict, text: str) -> dict:
    idx = DocIndex(text)
    out = dict(analysis)
    for key, _ in SINGLE_FIELDS:
        out[key] = verify_field(analysis[key], key, idx)
    out["discarded"] = []  # AI proposals thrown out by code, kept visible for the reviewer
    for key in [k for k, _ in LIST_FIELDS] + ["important_facts"]:
        kept = []
        for item in analysis[key]:
            v = dict(verify_field(item, key, idx), label=item["label"])
            if v["source"] == "missing":
                out["discarded"].append({"label": item["label"], "value": item["value"], "reason": v["note"]})
            else:
                kept.append(v)
        out[key] = kept
    for key, label in SINGLE_FIELDS:
        if out[key]["source"] == "missing" and analysis[key]["value"]:
            out["discarded"].append({"label": label, "value": analysis[key]["value"], "reason": out[key]["note"]})
    # Deterministic gap list (computed by code, not the AI).
    out["missing_fields"] = [label for key, label in SINGLE_FIELDS if out[key]["source"] == "missing"]
    out["stats"] = _stats(out)
    return out


def _all_items(a: dict) -> list[dict]:
    items = [a[k] for k, _ in SINGLE_FIELDS]
    for k, _ in LIST_FIELDS:
        items += a[k]
    return items + a["important_facts"]


def _stats(a: dict) -> dict:
    items = _all_items(a)
    s = {src: sum(1 for i in items if i["source"] == src) for src in ("explicit", "inferred", "missing")}
    s["low_confidence"] = sum(1 for i in items if i["confidence"] == "low")
    s["corrected"] = sum(1 for i in items if i.get("note") and i["source"] != "missing") + len(a["discarded"])
    return s


def review_rows(analysis: dict) -> list[dict]:
    """Flatten into the fields a human approves / edits / rejects.
    Core fields are always included (even missing ones, so a human can fill them in)."""
    rows = [{"key": key, "label": label, **analysis[key]} for key, label in SINGLE_FIELDS]
    for key, _ in LIST_FIELDS:
        rows += [{"key": f"{key}.{i}", **item} for i, item in enumerate(analysis[key])]
    return rows
