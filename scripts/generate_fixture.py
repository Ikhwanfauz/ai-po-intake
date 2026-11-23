from pathlib import Path
from shutil import copyfile

from reportlab.lib.colors import HexColor, white
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas


def generate(path, po_number="PO-103", second_price="RM45.50", *,
             first_price="RM100.00", first_quantity="2", reordered=False, edition=None,
             customer="CUST-101"):
    path.parent.mkdir(parents=True, exist_ok=True)
    pdf = canvas.Canvas(str(path), pagesize=A4, invariant=True)
    pdf.setTitle(f"{po_number} | Kencana Engineering")
    pdf.setAuthor("Kencana Engineering (synthetic)")
    pdf.setFillColor(HexColor("#132b37"))
    pdf.rect(0, 714, A4[0], 128, fill=1, stroke=0)
    pdf.setFillColor(white)
    pdf.setFont("Helvetica", 13)
    pdf.drawString(48, 791, "KENCANA ENGINEERING")
    pdf.setFont("Helvetica-Bold", 28)
    pdf.drawString(48, 744, "Purchase Order")
    pdf.setFont("Helvetica-Bold", 17)
    pdf.drawRightString(A4[0] - 48, 748, po_number)

    pdf.setFillColor(HexColor("#132b37"))
    pdf.setFont("Helvetica", 12)
    for y, text in [
        (674, f"PO number: {po_number}"),
        (652, f"Customer reference: {customer}"),
        (630, "Currency: MYR"),
        (608, "Supplier: Meridian Industrial Supply"),
    ]:
        pdf.drawString(48, y, text)

    pdf.setFillColor(HexColor("#e6f1ef"))
    pdf.rect(48, 525, A4[0] - 96, 29, fill=1, stroke=0)
    columns = (58, 166, 350, 406, 466)
    pdf.setFillColor(HexColor("#132b37"))
    pdf.setFont("Helvetica-Bold", 10)
    for x, label in zip(columns, ("SKU", "Description", "Qty", "Unit", "Unit price")):
        pdf.drawString(x, 536, label)
    pdf.setFont("Helvetica", 11)
    rows = [
        ("BRG-6204", "Deep groove bearing", first_quantity, "EA", first_price),
        ("BELT-A42", "Industrial V-belt", "3", "EA", second_price),
    ]
    if reordered:
        rows.reverse()
    for y, row in zip((504, 474), rows):
        for x, value in zip(columns, row):
            pdf.drawString(x, y, value)
        pdf.setStrokeColor(HexColor("#ccd9dc"))
        pdf.line(48, y - 12, A4[0] - 48, y - 12)
    pdf.setFont("Helvetica", 9)
    if edition:
        pdf.drawString(48, 426, f"Customer document: {edition}")
    pdf.drawString(48, 48, "Synthetic demo document")
    pdf.drawRightString(A4[0] - 48, 48, "Page 1 of 1")
    pdf.save()


if __name__ == "__main__":
    output = Path("output/pdf")
    generate(output / "PO-103.pdf")
    for version, price in ((1, "RM90.00"), (2, "RM100.00")):
        original = output / f"PO-104-V{version}.pdf"
        generate(original, "PO-104", first_price=price, edition=f"Revision {version}")
        copyfile(original, output / f"PO-104-V{version}-copy.pdf")
    generate(output / "PO-104-equivalent.pdf", "PO-104", "RM45.5",
             first_price="RM100.0", reordered=True, edition="Regenerated revision 2")
    generate(output / "PO-104-post-order.pdf", "PO-104", first_quantity="4", edition="Revision 3")
    generate(output / "PO-105-misread.pdf", "PO-105", first_price="RM90.00")
