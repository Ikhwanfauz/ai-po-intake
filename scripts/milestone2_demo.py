"""Replay known draft responses for the local Milestone 2 walkthrough."""

from functools import lru_cache
from pathlib import Path

from po_intake.api import create_app
from po_intake.evidence import LINE_FIELDS, source_pages
from po_intake.extraction import Draft, DraftLine, ExtractionOutput, Proposal, extract_with_ollama, pdf_pages


FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "output" / "pdf"
FIXTURE_NAMES = (
    "PO-104-V1.pdf", "PO-104-V2.pdf", "PO-104-equivalent.pdf",
    "PO-104-post-order.pdf", "PO-105-misread.pdf",
)


def page_key(pages):
    return tuple((page["page"], page["text"]) for page in pages)


@lru_cache(maxsize=1)
def fixture_names_by_text():
    return {page_key(pdf_pages((FIXTURE_ROOT / name).read_bytes())): name for name in FIXTURE_NAMES}


def extract_demo(settings, pages):
    fixture = fixture_names_by_text().get(page_key(pages))
    if fixture is None:
        return extract_with_ollama(settings, pages)

    source = next(source_pages(pages))
    headers = {}
    for name, cells in source.headers.items():
        cell = cells[0]
        start = source.text.rfind("\n", 0, cell.start) + 1
        headers[name] = Proposal(value=cell.value, page=source.number, excerpt=source.text[start:cell.end])
    lines = []
    for cells in source.rows.values():
        excerpt = source.text[cells["sku"].start:cells["unit_price"].end]
        fields = {name: Proposal(value=cells[name].value, page=source.number, excerpt=excerpt)
                  for name in LINE_FIELDS}
        if fixture == "PO-105-misread.pdf" and fields["sku"].value == "BRG-6204":
            fields["unit_price"].value = "RM100.00"
        lines.append(DraftLine(**fields, unit=cells["unit"].value))
    raw = Draft(**headers, lines=lines).model_dump_json()
    return ExtractionOutput(raw, "Demo fixture replay")


app = create_app(extractor=extract_demo)
