# DocFlow AI

Turn an unstructured business document (a service request, an estimate, an application, a claim letter) into
**structured, verified, human-approved data** that can go straight into another system.

A small prototype built to show one idea clearly: **AI extraction you can trust**.

> **The AI proposes. Code checks it against the document. A human verifies it.**

Every important value is stored with its **source** (EXPLICIT / INFERRED / MISSING), a **confidence**
(HIGH / MEDIUM / LOW) and **evidence**: an exact excerpt of the uploaded document, stored as character offsets.
Nothing is considered verified until a reviewer approves it.

---

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env          # then edit .env
python run.py                 # open http://127.0.0.1:5000
```

| `.env` setting | What it does |
|---|---|
| `ANTHROPIC_API_KEY` | Enables real AI analysis with Claude. |
| `LLM_MODEL` | Model name (default `claude-sonnet-5`). |
| `DEMO_MODE=true` | Shows the sample-document gallery (fictional data). With no API key, uses a clearly labeled offline rule-based analyzer so the demo always works. |
| `SECRET_KEY` | Signs session cookies. Generate one with `python -c "import secrets; print(secrets.token_hex(32))"`. |
| `MAX_UPLOAD_MB`, `MAX_PDF_PAGES`, `MAX_LLM_CHARS` | Safety and cost limits. |

Run the tests: `python -m unittest -v` (52 tests, no API key or network needed. The LLM is mocked.)
Test-only extra: `pip install reportlab` (used to generate PDFs in tests).

### Deploy to Render

The repo includes `render.yaml`, so Render can set up the service from it (a "Blueprint").

1. Push this project to a GitHub repository. Never commit `.env`, which `.gitignore` already excludes.
2. In Render: **New → Blueprint** → pick the repository → Render reads `render.yaml`.
3. Enter the two secrets it asks for: `ANTHROPIC_API_KEY` and `APP_PASSWORD`. `SECRET_KEY` is generated automatically.
4. Click **Apply**. When the build finishes, open the `.onrender.com` URL and sign in with any username and your `APP_PASSWORD`.

Production setup:
- `gunicorn` (2 workers × 4 threads, 120 s timeout for slow AI calls)
- `/healthz` health check
- HTTPS-only session cookies
- Password gate on every page

Free-plan notes:
- The service sleeps after about 15 minutes idle, and the first visit then takes about a minute.
- The SQLite file resets on every redeploy or restart. The sample documents always work, but saved reviews don't persist. For persistence, attach a Render Disk and point `DATABASE_PATH` at it, or move to Postgres.

---

## Architecture

```
docflow-ai/
├── run.py                 Entry point
├── docflow/
│   ├── __init__.py        App factory: config, CSRF, security headers, error pages
│   ├── config.py          Reads .env (secrets never hard-coded)
│   ├── routes.py          HTTP layer only: parse request → call pipeline/DB → render
│   ├── pipeline.py        The workflow in one function: validate → extract → analyze → store
│   ├── validation.py      Step 1: extension allowlist, size limit, magic-byte check, safe filename
│   ├── extraction.py      Step 2: TXT decoding, PDF text extraction, scanned-PDF detection
│   ├── analysis.py        Step 3: prompt + LLM call (forced structured output) + offline fallback
│   ├── schema.py          The JSON Schema + normalizer: one source of truth for the data shape
│   ├── grounding.py       Step 4: re-derives source, confidence and evidence from the document
│   ├── db.py              SQLite: documents, analyses, field_reviews
│   ├── demo_docs/         Fictional sample documents (TXT + PDF)
│   ├── templates/  static/
├── scripts/make_demo_pdf.py
└── tests/test_docflow.py
```

**Each module has one job.** Routes don't know how PDFs are parsed; the analyzer doesn't know about HTTP; the pipeline
doesn't know about HTML. That makes each piece easy to test, explain and replace.

### Database

| Table | Holds |
|---|---|
| `documents` | Metadata (safe filename, type, size, SHA-256, pages), extracted text, **review status** (`pending_review` / `approved` / `rejected`), reviewer note |
| `analyses` | The verified AI output as JSON, plus which provider and model produced it |
| `field_reviews` | One row per reviewable field. **AI proposal**: `ai_value`, `ai_source`, `ai_confidence`, `evidence`, `evidence_start`/`evidence_end`, `confidence_reason`, `system_note`. **Human decision**: `final_value`, `decision` (`pending` / `approved` / `edited` / `rejected`). The AI's proposal is never overwritten, which gives you an audit trail. |

Databases created by the first version are upgraded automatically on startup (`PRAGMA user_version`).

---

## The provenance model (anti-hallucination rules)

Every important field (customer, address, email, phone, document type, service, each date, each amount, each fact):

```json
{ "value": "(951) 555-0142", "source": "EXPLICIT", "confidence": "HIGH",
  "evidence": "You can reach me at (951) 555-0142", "evidence_offsets": [733, 767] }
```

| Source | Meaning | Rule enforced by code |
|---|---|---|
| **EXPLICIT** | Written in the document | The value itself must be found in the document text. |
| **INFERRED** | A reasonable deduction | Must have a real excerpt from the document as its basis. Never upgraded to explicit automatically. |
| **MISSING** | Not in the document | Anything with no support. Value, confidence and evidence are empty. |

**Evidence can't be invented.** The model's quote is only used as a *search key*. If it's found, we store the
**document's own characters** at that position plus their offsets. If it isn't found, there is no evidence. The
UI highlights evidence by offset, so what you see highlighted is exactly what was stored.

**Source is checked, not trusted.** `grounding.py` applies these rules:

| AI proposal | What DocFlow does |
|---|---|
| Explicit, excerpt found, value inside it | Keep EXPLICIT. Confidence up to HIGH. |
| Explicit, but the excerpt is wrong or fake and the value *is* in the document | Keep EXPLICIT. DocFlow locates the evidence itself. Max MEDIUM. |
| Explicit, but the value is reworded (not literally in the text) | Relabel **INFERRED**, with the excerpt as its basis. Max MEDIUM. |
| Inferred, with a real excerpt | Keep INFERRED. Max MEDIUM. |
| Anything with neither the value nor its excerpt in the document | **Discard → MISSING** |
| Name, address, email or phone not literally written | **Discard → MISSING** (identity and contact details are never guessed) |
| Any email or phone number anywhere (even inside a fact) that isn't in the document | **Discard → MISSING** |
| Document contains several different emails/phones | Max MEDIUM: "confirm this is the customer's" |
| Model says "N/A", "unknown", or labels a value missing | MISSING, with the value dropped |

**Confidence** = the model's self-rating *capped* by what code could verify. Code can only lower it, never raise it.
Every field stores a one-line `confidence_reason`. Every automatic correction stores a `system_note` and shows as
"Corrected by DocFlow". Discarded proposals stay listed under "Discarded by DocFlow" so the reviewer sees what the
AI tried to claim. They are never shown as data.

**Verified means human-verified.** Code checks only decide the labels. A value becomes verified only when a reviewer
approves or corrects it. The export's `verified_fields` contains only those values. The summary, next step, action
items and extra gaps are labeled "AI-generated · not evidence-checked".

**How the reviewer can tell the categories apart at a glance.**
- Each category has a colored badge and its own symbol (■ Explicit, ◈ Inferred, □ Missing), so color isn't the only cue.
- Each field row has a colored left rail: solid for explicit, dashed for inferred and missing. Inferred values are shown in italics on an amber background.
- Confidence is a 3-bar meter plus a word.
- Every evidence excerpt is pre-highlighted in the original text (green for evidence, amber for an inference's basis). Clicking either side jumps to the other.
- Filters show All / Explicit / Inferred / Missing / Needs attention.
- "Approve all explicit · high" bulk-approves only the safe fields. Inferred, medium, low and corrected fields each need an individual decision.

---

## How information travels through the app (the interview explanation)

```
 Browser                Flask                                                SQLite
 ───────                ─────                                                ──────
 upload ──► routes.upload
              └─► pipeline.process_document
                    1. validation   is it really a PDF/TXT, under the limit?  ──✗──► friendly error, nothing stored
                    2. extraction   bytes → plain text (file then discarded)   ──✗──► "scanned PDF" / "damaged" error
                    3. analysis     text → Claude, forced to fill our JSON schema
                    4. schema       normalize: any missing/wrong-typed key → safe default
                    5. grounding    re-derive source/confidence; evidence = exact slice of the text
                    6. db           document + analysis + review rows in ONE transaction ──► saved
 review page ◄── routes.document  (left: original text · right: verified analysis)
 approve/edit/reject ──► routes.review ──► field_reviews + documents.review_status ──► saved
 export.json ◄── routes.export    (verified_fields = only human-approved values → CRM/ERP)
```

In plain words:

1. **The user uploads a file.** Before reading it, we check three things: the extension is allowed, it's under the size
   limit, and **the bytes match the type** (a real PDF starts with `%PDF-`). The file is only read in memory. It is
   never saved to disk and never executed.
2. **We extract the text.** TXT is decoded, and PDFs go through `pypdf`. If a PDF has no text layer (a scan), we stop and
   say so instead of sending empty text to the AI.
3. **We send the text to the LLM** with strict rules. We don't just *ask* for JSON: we give the model a tool whose input
   is our JSON Schema and **force** it to call that tool, so the response is always structured. Every field comes back as
   `{value, source, confidence, evidence}`. The document is wrapped in `<document>` tags and treated as untrusted data
   (prompt-injection defense).
4. **We don't trust the AI's labels, we check them.** `grounding.py` looks up each excerpt and each value in the
   document text. It stores the document's own characters and offsets as evidence, relabels or discards anything
   unsupported, and caps confidence at what it could verify (see *The provenance model* above). The list of missing
   core fields is computed by code, not by the AI.
5. **Everything is saved in one database transaction**, so there's never a document without its analysis.
6. **A person reviews it.** Two columns: original text on the left, analysis on the right. For each field they
   approve, reject or edit (an edit counts as a correction and is stored alongside the AI's original value). They can also fill in
   missing fields. "Approve & complete" is blocked until every field has a decision.
7. **The export** (`/documents/<id>/export.json`) contains only human-approved values. That's the clean record that
   would go into the next system.

### Key design decisions (and why)

| Decision | Why |
|---|---|
| Forced tool call with a JSON Schema | More reliable than "please reply in JSON". The response always matches the expected structure. |
| Server-side normalizer (`schema.py`) | Even valid JSON can have wrong types or missing keys. The rest of the app always gets the same shape. |
| Evidence stored as document offsets (`grounding.py`) | LLMs can confidently misquote. Stored evidence is always a real slice of the document, so it can't be invented. |
| Code decides source and confidence | The model's labels are only a proposal. Code can lower confidence or relabel, never raise it. "Explicit" means the value is literally there. |
| Discard, don't downgrade, unsupported values | A weak guess shown in the UI still anchors the reviewer. If nothing in the document supports it, it's MISSING. |
| Human-in-the-loop with audit trail | AI proposes, a person decides. Corrections are stored next to the AI values, which is also data for measuring accuracy. |
| Don't store uploaded files | Smaller attack and privacy surface. We keep only the text we need plus a SHA-256 fingerprint. |
| Plain `sqlite3`, no ORM | Few tables. Every query is visible and easy to explain. |
| Synchronous processing | Simplest thing that works for a demo. `pipeline.py` has no HTTP dependency, so it can move to a background queue unchanged. |
| Offline analyzer only in demo mode | The demo never fails because of a missing key, and the UI clearly says "offline rules (not AI)". |

---

## Security

- API keys only from `.env` (git-ignored). Never logged, never rendered, and error messages never include them.
- Upload allowlist (PDF/TXT), `MAX_CONTENT_LENGTH` size limit (returns HTTP 413), magic-byte check, `secure_filename`, PDF page limit, encrypted-PDF rejection.
- Uploaded content is only parsed as data, never executed or saved to disk.
- CSRF token on every form. Jinja auto-escaping (document text can't inject HTML). Strict Content-Security-Policy (no inline scripts), `X-Frame-Options: DENY`, `nosniff`.
- Custom error pages that never show stack traces. Debug mode is off unless `FLASK_DEBUG=1`.
- Demo route only serves files that exist in `demo_docs/` (no path traversal).

## Limitations and next steps

- No OCR for scanned PDFs (add Tesseract or a vision model).
- Long documents are cut to `MAX_LLM_CHARS` (the UI says so). Next step: chunking.
- No user accounts or roles. Add authentication before any real use.
- Processing is synchronous. For volume, move `process_document` to a job queue.
- Next features: accuracy dashboard from `field_reviews` (AI proposal vs. human decision, by source and confidence), push the verified export to a CRM via API.

All sample documents describe fictional people and businesses (`example.com` emails, `555` phone numbers).
