"""DocFlow AI test suite - run with:  python -m unittest -v

Covers every pipeline stage plus the full HTTP workflow. The real LLM is replaced
by a fake HTTP response, so tests are fast, free and deterministic.
"""
import io
import json
import os
import re
import tempfile
import unittest
from unittest import mock

from pypdf import PdfWriter

from docflow import create_app
from docflow.analysis import AnalysisError, AnthropicAnalyzer, run_analysis
from docflow.extraction import ExtractionError, extract_text
from docflow.grounding import ground_analysis
from docflow.schema import normalize_analysis
from docflow.validation import ValidationError, validate_upload

DOC = """Invoice #1042
Bill to: Acme Bakery LLC
Contact: Sam Rivera, sam@acmebakery.example, (312) 555-0101
Service: Commercial oven repair
Due date: October 30, 2026
Total due: $1,240.00
"""

FAKE_API_KEY = "sk-ant-TEST-SECRET-should-never-leak"


def field(value, source, evidence, confidence="high"):
    if source == "missing":
        confidence = None
    return {"value": value, "source": source, "confidence": confidence, "evidence": evidence}


def llm_output(**overrides):
    """What a well-behaved LLM might return for DOC (before grounding)."""
    out = {
        "document_type": field("Invoice", "explicit", "Invoice #1042"),
        "customer_name": field("Acme Bakery LLC", "explicit", "Bill to: Acme Bakery LLC"),
        "address": field(None, "missing", None),
        "email": field("sam@acmebakery.example", "explicit", "sam@acmebakery.example"),
        "phone": field("(312) 555-0101", "explicit", "(312) 555-0101"),
        "requested_service": field("Commercial oven repair", "explicit", "Service: Commercial oven repair"),
        "important_dates": [{"label": "Due date", **field("October 30, 2026", "explicit", "Due date: October 30, 2026")}],
        "monetary_amounts": [{"label": "Total due", **field("$1,240.00", "explicit", "Total due: $1,240.00")}],
        "important_facts": [],
        "missing_information": ["Billing address"],
        "action_items": ["Confirm billing address"],
        "summary": "An invoice for oven repair.",
        "recommended_next_step": "Request the billing address.",
    }
    out.update(overrides)
    return out


def fake_response(status=200, tool_input=None):
    resp = mock.Mock(status_code=status)
    resp.json.return_value = {"content": [
        {"type": "tool_use", "name": "record_document_analysis", "input": tool_input or {}}]}
    return resp


def make_pdf(text_pages=None) -> bytes:
    """Build a PDF. With no text -> a blank page (simulates a scanned PDF)."""
    buf = io.BytesIO()
    if text_pages:
        from reportlab.pdfgen import canvas  # dev-only dependency for tests
        c = canvas.Canvas(buf)
        for page in text_pages:
            y = 780
            for line in page.splitlines():
                c.drawString(50, y, line)
                y -= 16
            c.showPage()
        c.save()
    else:
        w = PdfWriter()
        w.add_blank_page(612, 792)
        w.write(buf)
    return buf.getvalue()


# ------------------------------------------------------------------ unit tests

class ValidationTests(unittest.TestCase):
    MAX = 1024 * 1024

    def test_accepts_txt_and_pdf(self):
        self.assertEqual(validate_upload("a.txt", b"hello world", self.MAX).file_type, "txt")
        self.assertEqual(validate_upload("a.PDF", b"%PDF-1.7 ...", self.MAX).file_type, "pdf")

    def test_rejects_disallowed_extension(self):
        for name in ("run.exe", "macro.docm", "page.html", "script.py", "noext"):
            with self.assertRaises(ValidationError):
                validate_upload(name, b"data", self.MAX)

    def test_rejects_empty_and_oversize(self):
        with self.assertRaises(ValidationError):
            validate_upload("a.txt", b"", self.MAX)
        with self.assertRaises(ValidationError):
            validate_upload("a.txt", b"x" * (self.MAX + 1), self.MAX)

    def test_rejects_content_that_does_not_match_extension(self):
        with self.assertRaisesRegex(ValidationError, "real PDF"):
            validate_upload("invoice.pdf", b"MZ\x90\x00 fake exe", self.MAX)
        with self.assertRaisesRegex(ValidationError, "binary"):
            validate_upload("notes.txt", b"abc\x00\x01\x02", self.MAX)

    def test_sanitizes_filename(self):
        f = validate_upload("../../etc/pass wd.txt", b"hello", self.MAX)
        self.assertNotIn("/", f.safe_name)
        self.assertNotIn("..", f.safe_name)


class ExtractionTests(unittest.TestCase):
    def test_txt_utf8_and_legacy_encoding(self):
        self.assertIn("Café", extract_text("txt", "Menu: Café au lait, $4.50 per cup".encode("utf-8"), 30)[0])
        self.assertIn("Café", extract_text("txt", "Menu: Café au lait, $4.50 per cup".encode("cp1252"), 30)[0])

    def test_pdf_with_text(self):
        text, pages = extract_text("pdf", make_pdf(["Estimate for Jane Doe", "Total: $500.00"]), 30)
        self.assertIn("Jane Doe", text)
        self.assertEqual(pages, 2)

    def test_scanned_pdf_is_reported_not_sent_to_ai(self):
        with self.assertRaisesRegex(ExtractionError, "scanned"):
            extract_text("pdf", make_pdf(), 30)

    def test_corrupt_pdf_is_handled(self):
        with self.assertRaises(ExtractionError):
            extract_text("pdf", b"%PDF-1.4 this is not really a pdf", 30)

    def test_page_limit(self):
        with self.assertRaisesRegex(ExtractionError, "pages"):
            extract_text("pdf", make_pdf(["page one text here"] * 3), 2)


class SchemaTests(unittest.TestCase):
    def test_garbage_llm_output_becomes_valid_shape(self):
        a = normalize_analysis({"email": "not-a-dict", "important_dates": "nope", "summary": 42})
        self.assertEqual(a["email"], {"value": None, "source": "missing", "confidence": None, "evidence": None})
        self.assertEqual(a["important_dates"], [])
        self.assertEqual(a["summary"], "42")
        self.assertEqual(a["action_items"], [])

    def test_null_value_can_never_be_explicit(self):
        a = normalize_analysis({"phone": field(None, "explicit", "something")})
        self.assertEqual(a["phone"]["source"], "missing")
        self.assertIsNone(a["phone"]["confidence"])

    def test_value_labeled_missing_is_dropped_not_shown(self):
        a = normalize_analysis({"email": {"value": "x@y.example", "source": "missing"}})
        self.assertIsNone(a["email"]["value"])

    def test_placeholder_values_count_as_missing(self):
        for junk in ("N/A", "unknown", "Not provided", "null", "  "):
            self.assertEqual(normalize_analysis({"phone": field(junk, "explicit", junk)})["phone"]["source"], "missing")

    def test_unknown_labels_fall_back_to_weakest_claim(self):
        a = normalize_analysis({"document_type": {"value": "Invoice", "source": "certain", "confidence": "100%"}})
        self.assertEqual(a["document_type"]["source"], "inferred")
        self.assertEqual(a["document_type"]["confidence"], "low")


class GroundingTests(unittest.TestCase):
    """The core safety properties of the provenance layer."""

    def ground(self, text=DOC, **overrides):
        return ground_analysis(normalize_analysis(llm_output(**overrides)), text)

    # -- evidence can never be invented
    def test_evidence_is_an_exact_slice_of_the_document(self):
        a = self.ground()
        for key in ("customer_name", "email", "phone", "requested_service"):
            f = a[key]
            self.assertEqual(DOC[f["span"][0]:f["span"][1]], f["evidence"], key)
        d = a["important_dates"][0]
        self.assertEqual(DOC[d["span"][0]:d["span"][1]], d["evidence"])

    def test_evidence_uses_document_wording_not_models(self):
        # Model quotes with different case/punctuation -> stored evidence is the document's text.
        a = self.ground(customer_name=field("Acme Bakery LLC", "explicit", "BILL TO - acme bakery llc"))
        self.assertEqual(a["customer_name"]["evidence"], "Bill to: Acme Bakery LLC")
        self.assertEqual(a["customer_name"]["source"], "explicit")

    def test_fabricated_evidence_is_removed(self):
        a = self.ground(requested_service=field("Oven installation", "explicit", "Please install a new oven"))
        f = a["requested_service"]
        self.assertIsNone(f["evidence"])
        self.assertIsNone(f["span"])
        self.assertEqual(f["source"], "missing")
        self.assertIsNone(f["value"])
        self.assertIn("does not", f["note"])
        self.assertEqual(a["discarded"][0]["value"], "Oven installation")

    def test_fabricated_evidence_for_inferred_value_is_removed(self):
        a = self.ground(document_type=field("Invoice", "inferred", "This is an official invoice", "high"))
        self.assertIsNone(a["document_type"]["evidence"])
        self.assertEqual(a["document_type"]["source"], "missing")  # inference with no basis = guess

    def test_ellipsis_quotes_resolve_to_a_real_excerpt(self):
        # The two pieces are not adjacent in the document, so the whole quote can't match;
        # the piece that contains the value is used.
        a = self.ground(customer_name=field("Acme Bakery LLC", "explicit",
                                            "Due date: October 30, 2026 ... Bill to: Acme Bakery LLC"))
        self.assertEqual(a["customer_name"]["evidence"], "Bill to: Acme Bakery LLC")
        self.assertEqual(a["customer_name"]["confidence"], "high")

    def test_symbols_are_kept_in_evidence(self):
        a = self.ground(monetary_amounts=[{"label": "Total", **field("$1,240.00", "explicit", "1,240.00")}])
        self.assertEqual(a["monetary_amounts"][0]["evidence"], "$1,240.00")

    # -- source is checked, not trusted
    def test_paraphrased_explicit_value_is_relabeled_inferred(self):
        a = self.ground(requested_service=field("Fix the bakery oven", "explicit", "Service: Commercial oven repair"))
        f = a["requested_service"]
        self.assertEqual(f["source"], "inferred")
        self.assertEqual(f["confidence"], "medium")
        self.assertEqual(f["evidence"], "Service: Commercial oven repair")  # still a real basis

    def test_wrong_excerpt_but_value_present_stays_explicit_with_system_evidence(self):
        a = self.ground(email=field("sam@acmebakery.example", "explicit", "Total due: $1,240.00"))
        self.assertEqual(a["email"]["source"], "explicit")
        self.assertIn("sam@acmebakery.example", a["email"]["evidence"])
        self.assertEqual(a["email"]["confidence"], "medium")

    def test_inferred_is_never_upgraded_and_capped_at_medium(self):
        a = self.ground(document_type=field("Invoice", "inferred", "Invoice #1042", "high"))
        self.assertEqual(a["document_type"]["source"], "inferred")
        self.assertEqual(a["document_type"]["confidence"], "medium")

    def test_model_confidence_is_kept_when_fully_supported(self):
        a = self.ground(customer_name=field("Acme Bakery LLC", "explicit", "Bill to: Acme Bakery LLC", "medium"))
        self.assertEqual(a["customer_name"]["confidence"], "medium")  # never raised above the model's own rating

    # -- identity / contact details are never guessed
    def test_hallucinated_email_is_discarded(self):
        a = self.ground(email=field("billing@acme.example", "explicit", "billing@acme.example"))
        self.assertEqual(a["email"]["source"], "missing")
        self.assertIsNone(a["email"]["value"])
        self.assertIn("billing@acme.example", a["email"]["note"])

    def test_invented_contact_inside_a_fact_is_discarded_and_reported(self):
        a = self.ground(important_facts=[
            {"label": "Billing contact", **field("Email billing@acme.example", "inferred", "Bill to: Acme Bakery LLC", "medium")},
            {"label": "Payment", **field("Due October 30, 2026", "inferred", "Due date: October 30, 2026", "medium")}])
        self.assertEqual([f["label"] for f in a["important_facts"]], ["Payment"])
        self.assertEqual(a["discarded"][0]["label"], "Billing contact")
        self.assertIn("billing@acme.example", a["discarded"][0]["reason"])

    def test_inferred_address_is_discarded(self):
        a = self.ground(address=field("Chicago, IL", "inferred", "(312) 555-0101", "medium"))
        self.assertEqual(a["address"]["source"], "missing")
        self.assertIsNone(a["address"]["value"])

    def test_phone_matches_despite_formatting(self):
        a = self.ground(phone=field("312.555.0101", "explicit", "312-555-0101"))
        self.assertEqual(a["phone"]["source"], "explicit")
        self.assertEqual(a["phone"]["evidence"], "(312) 555-0101")

    def test_multi_line_address_is_found(self):
        text = "Customer: Ann Lee\n12 Main St\nSpringfield, IL 62704\n"
        a = self.ground(text=text, customer_name=field("Ann Lee", "explicit", "Customer: Ann Lee"),
                        address=field("12 Main St, Springfield, IL 62704", "explicit", "12 Main St, Springfield, IL 62704"),
                        email=field(None, "missing", None), phone=field(None, "missing", None),
                        important_dates=[], monetary_amounts=[])
        self.assertEqual(a["address"]["source"], "explicit")
        self.assertEqual(a["address"]["evidence"], "12 Main St\nSpringfield, IL 62704")

    def test_several_emails_lower_confidence(self):
        text = DOC + "Our billing office: accounts@ovenfix.example\n"
        a = self.ground(text=text)
        self.assertEqual(a["email"]["confidence"], "medium")
        self.assertIn("2 different emails", a["email"]["reason"])

    def test_short_values_are_not_matched_by_coincidence(self):
        # "89" appears inside "1989" - a whole-token match is required.
        a = self.ground(text="Founded in 1989. Contact: sam@acmebakery.example",
                        monetary_amounts=[{"label": "Fee", **field("$89", "explicit", "$89")}])
        self.assertEqual(a["monetary_amounts"], [])
        self.assertIn("$89", [d["value"] for d in a["discarded"]])

    def test_missing_fields_are_listed_deterministically(self):
        a = self.ground()
        self.assertIn("Address", a["missing_fields"])
        self.assertIsNone(a["address"]["confidence"])

    def test_stats(self):
        a = self.ground(requested_service=field("Oven installation", "explicit", "Please install a new oven"))
        self.assertEqual(a["stats"]["missing"], 2)
        self.assertEqual(a["stats"]["inferred"], 0)
        self.assertEqual(a["stats"]["corrected"], 1)

    def test_low_confidence_comes_from_model_self_rating(self):
        a = self.ground(document_type=field("Invoice", "inferred", "Invoice #1042", "low"))
        self.assertEqual(a["document_type"]["confidence"], "low")
        self.assertEqual(a["stats"]["low_confidence"], 1)


class AnthropicAnalyzerTests(unittest.TestCase):
    CFG = {"MAX_LLM_CHARS": 30000}

    def analyzer(self):
        return AnthropicAnalyzer(FAKE_API_KEY, "test-model", "https://example.invalid", 5)

    @mock.patch("docflow.analysis.requests.post")
    def test_forces_structured_tool_call(self, post):
        post.return_value = fake_response(tool_input=llm_output())
        analysis, meta = run_analysis(DOC, self.CFG, analyzer=self.analyzer())
        body = post.call_args.kwargs["json"]
        self.assertEqual(body["tool_choice"], {"type": "tool", "name": "record_document_analysis"})
        self.assertIn("<document>", body["messages"][0]["content"])
        self.assertEqual(analysis["email"]["value"], "sam@acmebakery.example")
        self.assertEqual(meta["provider"], "anthropic")

    @mock.patch("docflow.analysis.time.sleep")
    @mock.patch("docflow.analysis.requests.post")
    def test_retries_once_on_overload(self, post, _sleep):
        post.side_effect = [fake_response(529), fake_response(tool_input=llm_output())]
        run_analysis(DOC, self.CFG, analyzer=self.analyzer())
        self.assertEqual(post.call_count, 2)

    @mock.patch("docflow.analysis.requests.post")
    def test_auth_error_is_friendly_and_does_not_leak_key(self, post):
        post.return_value = fake_response(401)
        with self.assertRaises(AnalysisError) as ctx:
            run_analysis(DOC, self.CFG, analyzer=self.analyzer())
        self.assertNotIn(FAKE_API_KEY, str(ctx.exception))

    @mock.patch("docflow.analysis.requests.post")
    def test_long_documents_are_truncated(self, post):
        post.return_value = fake_response(tool_input=llm_output())
        _, meta = run_analysis(DOC * 1000, {"MAX_LLM_CHARS": 500}, analyzer=self.analyzer())
        self.assertTrue(meta["truncated"])
        self.assertLess(len(post.call_args.kwargs["json"]["messages"][0]["content"]), 600)


# ------------------------------------------------------------------ end-to-end

class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = create_app({"TESTING": True, "DATABASE_PATH": os.path.join(self.tmp.name, "t.db"),
                               "ANTHROPIC_API_KEY": FAKE_API_KEY, "DEMO_MODE": True,
                               "MAX_CONTENT_LENGTH": 64 * 1024, "MAX_UPLOAD_MB": 0})
        self.client = self.app.test_client()
        self.client.get("/")  # issues the CSRF token
        with self.client.session_transaction() as s:
            self.csrf = s["csrf_token"]
        patcher = mock.patch("docflow.analysis.requests.post",
                             return_value=fake_response(tool_input=llm_output()))
        self.post_mock = patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def upload(self, name, data):
        return self.client.post("/upload", data={"csrf_token": self.csrf, "document": (io.BytesIO(data), name)},
                                content_type="multipart/form-data")

    def test_full_workflow_upload_review_approve_export(self):
        # 1. Upload -> redirected to the review page
        r = self.upload("invoice.txt", DOC.encode())
        self.assertEqual(r.status_code, 302)
        page_url = r.headers["Location"]
        html = self.client.get(page_url).get_data(as_text=True)
        self.assertIn("Acme Bakery LLC", html)          # right column
        self.assertIn("Bill to: Acme Bakery LLC", html)  # left column (original text)
        self.assertIn("src-explicit", html)
        self.assertIn("src-missing", html)
        self.assertIn("Awaiting review", html)
        # Before any human action nothing is verified
        ex = self.client.get(page_url + "/export.json").get_json()
        self.assertEqual(ex["verified_fields"], {})
        self.assertFalse(ex["fields"]["email"]["human_verified"])
        self.assertEqual(ex["fields"]["email"]["ai_proposal"]["source"], "EXPLICIT")
        self.assertEqual(ex["fields"]["email"]["ai_proposal"]["confidence"], "HIGH")

        # 2. Approve without deciding every field -> refused
        r = self.client.post(page_url + "/review", data={"csrf_token": self.csrf, "action": "approve"},
                             follow_redirects=True)
        self.assertIn("still pending", r.get_data(as_text=True))

        # 3. Human decisions: approve most, correct the name, reject the phone, fill the missing address
        doc_id = int(page_url.rsplit("/", 1)[1])
        with self.app.app_context():
            from docflow.db import get_document
            _, _, reviews = get_document(doc_id)
        form = {"csrf_token": self.csrf, "action": "approve", "note": "Checked against PO."}
        for row in reviews:
            key = row["field_key"]
            form[f"value__{key}"] = row["final_value"] or ""
            form[f"decision__{key}"] = "approve"
        form["value__customer_name"] = "Acme Bakery, LLC"
        form["decision__phone"] = "reject"
        form["value__address"] = "12 Main St, Chicago, IL"
        r = self.client.post(page_url + "/review", data=form, follow_redirects=True)
        self.assertIn("document approved", r.get_data(as_text=True))

        # 4. Export reflects human corrections; AI originals preserved for audit
        ex = self.client.get(page_url + "/export.json").get_json()
        self.assertEqual(ex["review_status"], "approved")
        self.assertEqual(ex["verified_fields"]["customer_name"], "Acme Bakery, LLC")
        self.assertEqual(ex["fields"]["customer_name"]["decision"], "edited")
        self.assertEqual(ex["fields"]["customer_name"]["ai_proposal"]["value"], "Acme Bakery LLC")  # audit trail
        self.assertEqual(ex["fields"]["address"]["decision"], "edited")
        self.assertEqual(ex["fields"]["address"]["ai_proposal"]["source"], "MISSING")
        self.assertNotIn("phone", ex["verified_fields"])
        self.assertIsNone(ex["fields"]["phone"]["value"])
        self.assertTrue(ex["fields"]["email"]["human_verified"])
        self.assertIn("summary", ex["ai_generated_unverified"])
        self.assertEqual(ex["fields"]["phone"]["decision"], "rejected")
        self.assertEqual(ex["fields"]["email"]["decision"], "approved")

    def test_pdf_upload(self):
        r = self.upload("invoice.pdf", make_pdf([DOC]))
        self.assertEqual(r.status_code, 302)
        self.assertIn("/documents/", r.headers["Location"])

    def test_rejected_upload_shows_error_and_stores_nothing(self):
        r = self.upload("malware.exe", b"MZ....")
        self.assertEqual(r.status_code, 302)
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("not supported", html)
        self.assertIn("No documents yet", html)
        self.post_mock.assert_not_called()

    def test_oversize_upload_returns_413(self):
        r = self.upload("big.txt", b"a" * (70 * 1024))
        self.assertEqual(r.status_code, 413)

    def test_llm_failure_is_shown_gracefully(self):
        self.post_mock.return_value = fake_response(500)
        with mock.patch("docflow.analysis.time.sleep"):
            r = self.upload("invoice.txt", DOC.encode())
        html = self.client.get(r.headers["Location"]).get_data(as_text=True)
        self.assertIn("AI service returned an error", html)

    def test_csrf_required(self):
        r = self.client.post("/upload", data={"document": (io.BytesIO(b"hi there"), "a.txt")},
                             content_type="multipart/form-data")
        self.assertEqual(r.status_code, 400)

    def test_demo_documents_all_process(self):
        from docflow.routes import DEMO_DIR
        names = [n for n in os.listdir(DEMO_DIR) if n.endswith((".txt", ".pdf"))]
        self.assertGreaterEqual(len(names), 4)
        for name in names:
            r = self.client.post(f"/demo/{name}", data={"csrf_token": self.csrf})
            self.assertIn("/documents/", r.headers["Location"], name)

    def test_demo_route_rejects_path_traversal(self):
        for name in ("..%2F..%2Fconfig.py", "../__init__.py", "nope.txt"):
            r = self.client.post(f"/demo/{name}", data={"csrf_token": self.csrf})
            self.assertIn(r.status_code, (404, 405))

    def test_security_headers_and_no_secret_exposure(self):
        r = self.client.get("/")
        self.assertIn("default-src 'self'", r.headers["Content-Security-Policy"])
        self.assertEqual(r.headers["X-Frame-Options"], "DENY")
        self.assertNotIn(FAKE_API_KEY, r.get_data(as_text=True))

    def test_document_text_is_html_escaped(self):
        r = self.upload("xss.txt", b"<script>alert('x')</script> Customer: Bob, bob@example.com")
        html = self.client.get(r.headers["Location"]).get_data(as_text=True)
        self.assertNotIn("<script>alert", html)
        self.assertIn("&lt;script&gt;", html)

    def test_provenance_is_persisted_per_field(self):
        self.post_mock.return_value = fake_response(tool_input=llm_output(
            requested_service=field("Oven installation", "explicit", "Please install a new oven")))
        r = self.upload("invoice.txt", DOC.encode())
        doc_id = int(r.headers["Location"].rsplit("/", 1)[1])
        with self.app.app_context():
            from docflow.db import get_document
            doc, _, reviews = get_document(doc_id)
        rows = {row["field_key"]: row for row in reviews}
        email = rows["email"]
        self.assertEqual((email["ai_source"], email["ai_confidence"]), ("explicit", "high"))
        self.assertEqual(doc["extracted_text"][email["evidence_start"]:email["evidence_end"]], email["evidence"])
        svc = rows["requested_service"]
        self.assertEqual((svc["ai_source"], svc["ai_confidence"], svc["evidence"], svc["ai_value"]),
                         ("missing", None, None, None))
        self.assertIsNotNone(svc["system_note"])
        self.assertEqual(rows["address"]["ai_source"], "missing")
        html = self.client.get(r.headers["Location"]).get_data(as_text=True)
        self.assertIn("Corrected by DocFlow", html)
        self.assertNotIn("Please install a new oven\u201d\n", html)

    def test_v1_database_is_migrated(self):
        import sqlite3
        path = os.path.join(self.tmp.name, "old.db")
        conn = sqlite3.connect(path)
        conn.executescript("""CREATE TABLE field_reviews (id INTEGER PRIMARY KEY, document_id INTEGER,
            field_key TEXT, label TEXT, ai_value TEXT, ai_status TEXT NOT NULL, final_value TEXT,
            decision TEXT NOT NULL DEFAULT 'pending', updated_at TEXT NOT NULL);
            INSERT INTO field_reviews VALUES (1, 1, 'email', 'Email', 'a@b.example', 'found', 'a@b.example', 'pending', 'x');""")
        conn.commit(); conn.close()
        create_app({"TESTING": True, "DATABASE_PATH": path})
        conn = sqlite3.connect(path)
        row = conn.execute("SELECT ai_source, ai_confidence FROM field_reviews").fetchone()
        self.assertEqual(row, ("explicit", None))
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 2)
        conn.close()

    def test_password_gate(self):
        import base64
        app = create_app({"TESTING": True, "DATABASE_PATH": os.path.join(self.tmp.name, "pw.db"),
                          "APP_PASSWORD": "open-sesame"})
        c = app.test_client()
        r = c.get("/")
        self.assertEqual(r.status_code, 401)
        self.assertIn("Basic", r.headers["WWW-Authenticate"])
        bad = base64.b64encode(b"demo:wrong").decode()
        self.assertEqual(c.get("/", headers={"Authorization": f"Basic {bad}"}).status_code, 401)
        good = base64.b64encode(b"anyone:open-sesame").decode()
        self.assertEqual(c.get("/", headers={"Authorization": f"Basic {good}"}).status_code, 200)
        # Health check stays public for Render, and reveals nothing sensitive
        r = c.get("/healthz")
        self.assertEqual((r.status_code, r.get_json()), (200, {"status": "ok"}))

    def test_unknown_document_404(self):
        self.assertEqual(self.client.get("/documents/9999").status_code, 404)


if __name__ == "__main__":
    unittest.main()
