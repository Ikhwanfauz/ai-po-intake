import json
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from po_intake import worker
from po_intake.api import create_app
from po_intake.config import Settings
from po_intake.db import GraphRun, Notification, SalesOrder
from po_intake.extraction import Draft, TransientExtractionError, pdf_pages
from po_intake.extraction_graph import Decision, Patch, evidence_trigger, run_graph
from test_milestone2 import approval, detail, intake_action


DEMO = Path("output/pdf/milestone4/demo")
MULTIPAGE = DEMO / "PO-201-multipage.pdf"
AMBIGUOUS = DEMO / "PO-202-ambiguous.pdf"
ACCEPT = {"action": "accept", "reason": "Both rows have exact evidence.", "targets": []}
ESCALATE = {"action": "escalate", "reason": "The buyer has not selected a price option.", "targets": []}
REREAD = {"action": "reread", "reason": "Page 2 supplies the missing continuation price.",
          "targets": [{"page": 2, "field": "lines.2.unit_price", "reason": "Read the Unit price column."}]}
PRICE_PATCH = {"changes": [{
    "field": "lines.2.unit_price",
    "proposal": {"value": "RM45.50", "page": 2, "excerpt": "BELT-A42 3 RM45.50"},
}]}


def proposal(value, page, excerpt, alternatives=()):
    return {"value": value, "page": page, "excerpt": excerpt, "alternatives": list(alternatives)}


def draft_for(*, missing=False, ambiguous=False, first_price="RM100.00"):
    po = "PO-202" if ambiguous else "PO-201"
    row = {
        "sku": proposal("BRG-6204", 1, f"BRG-6204 2 {first_price}"),
        "quantity": proposal("2", 1, f"BRG-6204 2 {first_price}"),
        "unit_price": proposal(first_price, 1, f"BRG-6204 2 {first_price}"),
    }
    lines = [row]
    if ambiguous:
        row["unit_price"] = proposal(None, 1, "BRG-6204 2 RM100.00 RM90.00", ("RM100.00", "RM90.00"))
    else:
        lines.append({
            "sku": proposal("BELT-A42", 2, "BELT-A42 3 RM45.50"),
            "quantity": proposal("3", 2, "BELT-A42 3 RM45.50"),
            "unit_price": proposal(None if missing else "RM45.50", 2, "BELT-A42 3 RM45.50"),
        })
    return Draft.model_validate({
        "po_number": proposal(po, 1, f"PO number: {po}"),
        "customer_reference": proposal("CUST-101", 1, "Customer reference: CUST-101"),
        "currency": proposal("MYR", 1, "Currency: MYR"), "lines": lines,
    })


class Stub:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.calls = []

    def __call__(self, settings, schema, prompt, payload, *, timeout, max_tokens):
        self.calls.append((schema, payload, timeout))
        value = next(self.responses)
        if isinstance(value, Exception):
            raise value
        return json.dumps(value)


def test_accept_has_two_distinct_model_calls():
    pages = pdf_pages(MULTIPAGE.read_bytes())
    chat = Stub(ACCEPT)
    calls = []

    def extraction(_, numbered_pages):
        calls.append(numbered_pages)
        return draft_for().model_dump_json()

    result = run_graph(Settings(), pages, "staff requested", baseline=extraction, chat=chat)
    assert len(calls) == 1
    assert chat.calls[0][:2] == (Decision, {
        "pages": pages, "proposal": result.draft.model_dump(mode="json"),
        "evidence_issues": [], "reread_available": True,
    })
    assert result.trace["decision"] == "accept"
    assert result.trace["rereads"] == 0 and result.trace["call_count"] == 2


@pytest.mark.parametrize("last_decision", [ACCEPT, REREAD], ids=["accept-patch", "second-reread-blocked"])
def test_scoped_reread_is_rechecked_at_most_once(last_decision):
    pages = pdf_pages(MULTIPAGE.read_bytes())
    initial = draft_for(missing=True)
    chat = Stub(REREAD, PRICE_PATCH, last_decision)
    settings = replace(Settings(), graph_timeout=7)
    result = run_graph(settings, pages, "missing price", initial=initial, chat=chat, initial_elapsed_ms=5)
    assert [call[0] for call in chat.calls] == [Decision, Patch, Decision]
    assert chat.calls[1][1] == {
        "pages": [pages[1]],
        "targets": [{"page": 2, "field": "lines.2.unit_price", "reason": "Read the Unit price column.",
                     "row_sku": "BELT-A42", "prior_value": None}],
    }
    assert all(call[2] == 7 for call in chat.calls)
    assert chat.calls[2][1]["reread_available"] is False
    assert result.draft.lines[1].unit_price.value == "RM45.50"
    assert initial.lines[1].unit_price.value is None
    assert result.trace["call_count"] == 4 and result.trace["rereads"] == 1
    assert result.trace["decision"] == ("accept" if last_decision is ACCEPT else "escalate")


@pytest.mark.parametrize("response", [
    {"action": "reread", "reason": "No scope"},
    {"action": "reread", "reason": "Bogus page",
     "targets": [{"page": 99, "field": "lines.2.unit_price", "reason": "Evidence"}]},
    TransientExtractionError("Timed out"),
    ACCEPT,
], ids=["invalid-decision", "invalid-page", "timeout", "backend-veto"])
def test_review_failure_or_unsupported_accept_retains_initial_proposal(response):
    pages = pdf_pages(MULTIPAGE.read_bytes())
    initial = draft_for(missing=True)
    chat = Stub(response)
    result = run_graph(Settings(), pages, "initial issue", initial=initial, chat=chat)
    assert result.draft == initial
    assert result.trace["decision"] == "escalate"
    assert result.trace["call_count"] == 2 and result.trace["rereads"] == 0
    assert result.trace["evidence_issues"]


@pytest.mark.parametrize("patch", [
    {"changes": [{"field": "currency", "proposal": {"value": "MYR", "page": 2, "excerpt": "MYR"}}]},
    {"changes": [{"field": "lines.2.unit_price",
                  "proposal": {"value": "RM100.00", "page": 2, "excerpt": "Fake quote"}}]},
    TransientExtractionError("Timed out"),
], ids=["out-of-scope", "unsupported-price", "reread-timeout"])
def test_bad_patch_leaves_last_usable_draft(patch):
    pages = pdf_pages(MULTIPAGE.read_bytes())
    initial = draft_for(missing=True)
    result = run_graph(Settings(), pages, "missing price", initial=initial, chat=Stub(REREAD, patch))
    assert result.draft == initial
    assert result.trace["decision"] == "escalate" and result.trace["rereads"] == 1
    assert result.trace["call_count"] == 3


def test_second_review_failure_keeps_the_source_backed_patch_but_escalates():
    pages = pdf_pages(MULTIPAGE.read_bytes())
    result = run_graph(Settings(), pages, "missing price", initial=draft_for(missing=True),
                       chat=Stub(REREAD, PRICE_PATCH, TransientExtractionError("Timed out")))
    assert result.draft.lines[1].unit_price.value == "RM45.50"
    assert result.trace["decision"] == "escalate"
    assert result.trace["call_count"] == 4 and not result.trace["evidence_issues"]


def test_reread_can_recover_an_omitted_row():
    pages = pdf_pages(MULTIPAGE.read_bytes())
    full = draft_for()
    omitted = full.model_copy(update={"lines": full.lines[:1]})
    fields = ("sku", "quantity", "unit_price")
    targets = [{"page": 2, "field": f"lines.2.{name}", "reason": "Missing continuation row"} for name in fields]
    patch = {"changes": [{"field": f"lines.2.{name}",
                         "proposal": getattr(full.lines[1], name).model_dump(mode="json")} for name in fields]}
    result = run_graph(Settings(), pages, "omitted row", initial=omitted,
                       chat=Stub({"action": "reread", "reason": "Omitted row", "targets": targets}, patch, ACCEPT))
    assert result.trace["decision"] == "accept"
    assert result.draft == full


def upload_pdf(client, path):
    response = client.post("/intakes", files={"file": (path.name, path.read_bytes(), "application/pdf")})
    response.raise_for_status()
    return response.json()


def get_runs(client, receipt):
    response = client.get(f"/intakes/{receipt['id']}/graph-runs")
    response.raise_for_status()
    return response.json()


def test_automatic_review_is_persisted_without_creating_an_order(database):
    settings, engine = database
    settings = replace(settings, foreground=False, graph_auto=True)
    chat = Stub(REREAD, PRICE_PATCH, ACCEPT)
    with TestClient(create_app(settings)) as client:
        receipt = upload_pdf(client, MULTIPAGE)
        assert worker.run_one(engine, settings, lambda *_: draft_for(missing=True).model_dump_json(), graph_chat=chat)
        saved = detail(client, receipt)
        run = get_runs(client, receipt)[0]
        assert run["source_snapshot_id"] == saved["snapshot"]["id"]
        assert run["mode"] == "automatic" and run["trace"]["rereads"] == 1
        assert run["trace"]["initial_proposal"]["lines"][1]["unit_price"]["value"] is None
        assert saved["snapshot"]["draft"]["lines"][1]["unit_price"]["value"] == "RM45.50"
        assert saved["po"]["status"] == "ready_for_approval"
        assert client.get("/orders").json() == []


def test_genuine_ambiguity_escalates_and_revalidation_cannot_clear_the_hold(database):
    settings, engine = database
    settings = replace(settings, foreground=False, graph_auto=True)
    with TestClient(create_app(settings)) as client:
        receipt = upload_pdf(client, AMBIGUOUS)
        assert worker.run_one(engine, settings, lambda *_: draft_for(ambiguous=True).model_dump_json(),
                              graph_chat=Stub(ESCALATE))
        saved = detail(client, receipt)
        assert saved["po"]["status"] == "needs_review"
        assert get_runs(client, receipt)[0]["trace"]["reason"] == ESCALATE["reason"]
        assert saved["snapshot"]["draft"]["lines"][0]["unit_price"]["alternatives"] == ["RM100.00", "RM90.00"]
        checked = intake_action(client, receipt, "validate", approval(saved))
        assert checked["snapshot"]["assessment"]["graph_issues"]
        intake_action(client, receipt, "approve", approval(saved), code=409)
        assert client.get("/orders").json() == []


def test_verified_price_mismatch_does_not_route_into_graph(database):
    settings, engine = database
    from scripts.milestone2_demo import extract_demo
    from po_intake.extraction import ExtractionOutput

    fixture = Path("output/pdf/PO-104-V1.pdf")
    settings = replace(settings, foreground=False, graph_auto=True)

    def baseline(current_settings, pages):
        result = extract_demo(current_settings, pages)
        assert isinstance(result, ExtractionOutput)
        return result.raw

    with TestClient(create_app(settings)) as client:
        receipt = upload_pdf(client, fixture)
        empty = Stub()
        assert worker.run_one(engine, settings, baseline, graph_chat=empty)
        values = detail(client, receipt)["snapshot"]["assessment"]
        assert values["validation"]["price_issues"] and not values["evidence"]["issues"]
        assert get_runs(client, receipt) == []
        assert empty.calls == []


def test_staff_candidate_preserves_active_snapshot_and_outbox_until_explicit_adoption(database):
    settings, engine = database
    baseline = lambda *_: draft_for().model_dump_json()
    with TestClient(create_app(settings, extractor=baseline, graph_chat=Stub(ACCEPT, ACCEPT))) as client:
        receipt = upload_pdf(client, MULTIPAGE)
        saved = intake_action(client, receipt, "process")
        expected = {"snapshot_id": saved["snapshot"]["id"], "active_snapshot_id": saved["po"]["active_snapshot_id"]}
        first = intake_action(client, receipt, "graph-runs", expected, code=201)
        second = intake_action(client, receipt, "graph-runs", expected, code=201)
        assert (first["mode"], first["trace"]["decision"]) == ("staff", "accept")
        assert detail(client, receipt)["snapshot"]["id"] == expected["snapshot_id"]
        with Session(engine) as db:
            assert db.scalar(select(func.count()).select_from(GraphRun)) == 2
            assert db.scalar(select(func.count()).select_from(Notification)) == 0
            assert db.scalar(select(func.count()).select_from(SalesOrder)) == 0
        result = intake_action(client, receipt, f"graph-runs/{first['id']}/adopt",
                               {**expected, "reason": "Verified against both PDF pages"})
        assert result["snapshot"]["id"] != expected["snapshot_id"]
        assert result["snapshot"]["parent_id"] == expected["snapshot_id"]
        assert get_runs(client, receipt)[1]["adopted_snapshot_id"] == result["snapshot"]["id"]
        intake_action(client, receipt, f"graph-runs/{second['id']}/adopt",
                      {**expected, "reason": "An old staff tab"}, code=409)
        assert client.get("/orders").json() == []


def test_graph_cannot_turn_genuinely_ambiguous_pdf_into_a_verified_price():
    pages = pdf_pages(AMBIGUOUS.read_bytes())
    initial = draft_for(ambiguous=True)
    assert evidence_trigger(initial, pages)
    result = run_graph(Settings(), pages, "ambiguous", initial=initial, chat=Stub(ACCEPT))
    assert result.trace["decision"] == "escalate"
    assert result.draft.lines[0].unit_price.value is None
