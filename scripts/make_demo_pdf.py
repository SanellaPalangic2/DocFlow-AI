"""Regenerates the fictional demo PDF (requires: pip install reportlab)."""
import os
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

OUT = os.path.join(os.path.dirname(__file__), "..", "docflow", "demo_docs", "roofing_estimate.pdf")

LINES = [
    ("Helvetica-Bold", 16, "SUMMIT RIDGE ROOFING CO."),
    ("Helvetica", 10, "License #RC-448210  |  office@summitridge.example  |  (720) 555-0187"),
    ("Helvetica", 10, ""),
    ("Helvetica-Bold", 13, "ESTIMATE #E-3391"),
    ("Helvetica", 10, "Estimate date: October 2, 2026"),
    ("Helvetica", 10, "Valid until: November 1, 2026"),
    ("Helvetica", 10, ""),
    ("Helvetica-Bold", 11, "Prepared for"),
    ("Helvetica", 10, "Customer: Linda Park"),
    ("Helvetica", 10, "Property: 905 Aspen Way, Golden, CO 80401"),
    ("Helvetica", 10, ""),
    ("Helvetica-Bold", 11, "Scope of work"),
    ("Helvetica", 10, "Service requested: Full roof replacement after hail damage"),
    ("Helvetica", 10, "Tear-off of existing asphalt shingles (approx. 24 squares)       $3,120.00"),
    ("Helvetica", 10, "Install synthetic underlayment and ice & water shield            $2,280.00"),
    ("Helvetica", 10, "Install Class 4 impact-resistant architectural shingles           $9,960.00"),
    ("Helvetica", 10, "Replace ridge vents and flashing                                  $1,140.00"),
    ("Helvetica", 10, "Disposal and permit fees                                            $650.00"),
    ("Helvetica", 10, ""),
    ("Helvetica-Bold", 11, "Total estimate: $17,150.00"),
    ("Helvetica", 10, "Deposit required to schedule (30%): $5,145.00"),
    ("Helvetica", 10, ""),
    ("Helvetica-Bold", 11, "Notes"),
    ("Helvetica", 10, "Customer indicated an insurance claim has been filed; claim number not yet provided."),
    ("Helvetica", 10, "Decking replacement, if required after tear-off, is billed at $85.00 per sheet."),
    ("Helvetica", 10, "Estimated project duration: 3 working days, weather permitting."),
    ("Helvetica", 10, ""),
    ("Helvetica", 10, "Customer signature: ______________________    Date: ____________"),
]

c = canvas.Canvas(OUT, pagesize=letter)
c.setTitle("Estimate E-3391 (fictional demo)")
y = 740
for font, size, text in LINES:
    c.setFont(font, size)
    c.drawString(60, y, text)
    y -= size + 8
c.save()
print("wrote", os.path.abspath(OUT))
