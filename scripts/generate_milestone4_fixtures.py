"""Generate the synthetic demo PDFs and the independently labeled evaluation set."""
import hashlib
import json
from pathlib import Path

from reportlab.lib.colors import HexColor, white
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas


ROOT = Path("output/pdf/milestone4")
BEARING = ("BRG-6204", "2", "RM100.00")
BELT = ("BELT-A42", "3", "RM45.50")


def render(path, po, tables, *, style, customer="CUST-101", currency="MYR", alternate_po=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    pdf = canvas.Canvas(str(path), pagesize=A4, invariant=True)
    pdf.setTitle(f"{path.stem} synthetic PO")
    for index, table in enumerate(tables, 1):
        demo = style == "demo"
        pdf.setFillColor(HexColor("#113544" if demo else "#353141"))
        pdf.rect(0, 750, A4[0], 92, fill=1, stroke=0)
        pdf.setFillColor(white)
        pdf.setFont("Helvetica-Bold", 21 if demo else 16)
        pdf.drawString(46, 786, "Kencana Engineering | Purchase order" if demo else "Purchase order")
        pdf.setFont("Helvetica", 10)
        pdf.drawRightString(548, 766, f"Page {index} of {len(tables)}")
        pdf.setFillColor(HexColor("#182830"))
        pdf.setFont("Helvetica", 12)
        if index == 1:
            headers = [f"PO number: {po}"] if po else []
            if alternate_po:
                headers.append(f"PO number: {alternate_po}")
            headers.extend((f"Customer reference: {customer}", f"Currency: {currency}"))
            for y, text in zip((716, 692, 668, 644), headers):
                pdf.drawString(46, y, text)
        else:
            pdf.drawString(46, 712, "Continuation of the order lines")
        ambiguous = len(table[0]) == 4
        columns = ("SKU", "Qty", "Option A (RM)", "Option B (RM)") if ambiguous else ("SKU", "Qty", "Unit price")
        y = 603 if index == 1 else 665
        pdf.setFillColor(HexColor("#e5efee" if demo else "#eeebf1"))
        pdf.rect(46, y - 10, 503, 28, fill=1, stroke=0)
        pdf.setFillColor(HexColor("#182830"))
        positions = (56, 235, 333, 455) if demo or style == "grid" else (64, 215, 316, 455)
        pdf.setFont("Helvetica-Bold", 11)
        for x, label in zip(positions, columns):
            pdf.drawString(x, y, label)
        pdf.setFont("Helvetica", 11)
        for number, row in enumerate(table, 1):
            for x, value in zip(positions, row):
                pdf.drawString(x, y - 32 * number, value)
        if ambiguous:
            pdf.drawString(46, y - 110, "Both options are quoted. The buyer has not selected an option.")
        pdf.setFont("Helvetica", 9)
        pdf.drawString(46, 45, "Synthetic evaluation document" if not demo else "Synthetic staff walkthrough")
        pdf.showPage()
    pdf.save()


def build():
    render(ROOT / "demo" / "PO-201-multipage.pdf", "PO-201", [[BEARING], [BELT]], style="demo")
    render(ROOT / "demo" / "PO-202-ambiguous.pdf", "PO-202",
           [[("BRG-6204", "2", "RM100.00", "RM90.00")]], style="demo")

    cases = [
        ("E01", "clean", [[BEARING, BELT]], "ready", {}),
        ("E02", "clean", [[("BELT-A42", "5", "RM45.5")]], "ready", {"style": "grid"}),
        ("E03", "multipage", [[BEARING], [BELT]], "ready", {"style": "grid"}),
        ("E04", "multipage alias", [[("6204-BRG", "7", "RM100")], [BELT]], "ready", {}),
        ("E05", "price mismatch", [[("BRG-6204", "2", "RM90.00")]], "business_review", {}),
        ("E06", "multipage price mismatch", [[BELT], [("BRG-6204", "4", "RM102.00")]], "business_review",
         {"style": "grid"}),
        ("E07", "missing PO", [[BEARING]], "extraction_review", {"po": None}),
        ("E08", "missing quantity", [[BEARING], [("BELT-A42", "TBD", "RM45.50")]], "extraction_review",
         {"style": "grid"}),
        ("E09", "unselected price options", [[("BRG-6204", "2", "RM100.00", "RM90.00")]], "extraction_review", {}),
        ("E10", "two labeled PO numbers", [[BELT]], "extraction_review", {"alternate_po": "PO-E10-B"}),
        ("E11", "repeated SKU", [[BEARING, ("BRG-6204", "5", "RM100.00")]], "ready", {"style": "grid"}),
        ("E12", "unsupported price precision", [[("BRG-6204", "3", "RM100.005")]], "extraction_review", {}),
    ]
    manifest = {"version": "m4-held-out-v1", "cases": []}
    for case_id, category, tables, route, options in cases:
        filename = f"{case_id}.pdf"
        path = ROOT / "held-out" / filename
        po = options.get("po", f"PO-{case_id}")
        render(path, po, tables, style=options.get("style", "ledger"), alternate_po=options.get("alternate_po"))
        lines = []
        for page, table in enumerate(tables, 1):
            for row in table:
                sku, qty, price, *choices = row
                lines.append({
                    "page": page, "sku": sku, "quantity": int(qty) if qty.isdigit() else None,
                    "unit_price": None if choices else price.removeprefix("RM"),
                    "printed_row": " ".join(row), "price_alternatives": [price, *choices] if choices else [],
                })
        manifest["cases"].append({
            "id": case_id, "category": category, "file": filename,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "expected_route": route,
            "headers": {"po_number": None if options.get("alternate_po") else po,
                        "customer_reference": "CUST-101", "currency": "MYR"},
            "lines": lines,
        })
    (ROOT / "held-out" / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    build()
