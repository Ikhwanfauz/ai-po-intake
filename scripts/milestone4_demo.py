"""Seed one missing price for the live, repeatable PO-201 handoff walkthrough."""
from functools import lru_cache
from pathlib import Path

from po_intake.extraction import Draft, ExtractionOutput, Proposal, extract_with_ollama, pdf_pages


@lru_cache(maxsize=1)
def demo_pages():
    fixture = Path(__file__).resolve().parents[1] / "output/pdf/milestone4/demo/PO-201-multipage.pdf"
    return pdf_pages(fixture.read_bytes())


def extract_demo(settings, pages):
    raw = extract_with_ollama(settings, pages)
    if pages != demo_pages():
        return raw
    draft = Draft.model_validate_json(raw)
    line = next((line for line in draft.lines if line.sku.value == "BELT-A42"), None)
    if line is None:
        return raw
    line.unit_price = Proposal(page=2)
    return ExtractionOutput(
        draft.model_dump_json(), "Ollama walkthrough: withheld the printed page 2 unit price.", original_raw=raw
    )
