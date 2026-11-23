import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from po_intake.api import create_app
from po_intake.db import Attempt, Intake, PurchaseOrder, SalesOrder, Snapshot, Version
from po_intake.evidence import verify
from po_intake.extraction import Draft, pdf_pages, store_pdf
from po_intake.review import diff_lines
from po_intake.seed import seed
from scripts.milestone2_demo import extract_demo


PDFS = Path("output/pdf")


def proposal(value, quote, page=1, alternatives=None):
    return {"value": value, "excerpt": quote, "page": page, "alternatives": alternatives or []}


def fixture_draft(pages):
    text = pages[0]["text"]
    data = {}
    for field, label in (("po_number", "PO number"), ("customer_reference", "Customer reference"),
                         ("currency", "Currency")):
        source = next(line for line in text.splitlines() if line.startswith(label + ":"))
        data[field] = proposal(source.split(": ", 1)[1], source)
    data["lines"] = []
    for row in text.splitlines():
        match = re.match(r"\s*(BRG-6204|BELT-A42)\s{2,}.*?\s{2,}(\d+)\s{2,}(EA)\s{2,}(RM[\d.]+)\s*$", row)
        if match:
            data["lines"].append({
                "sku": proposal(match[1], row.strip()), "quantity": proposal(match[2], row.strip()),
                "unit": match[3], "unit_price": proposal(match[4], row.strip()),
            })
    return data


def fixture_runner(_, pages):
    return json.dumps(fixture_draft(pages))


def upload(client, name, context=None):
    response = client.post("/intakes", data={"customer_context": context or ""},
                           files={"file": (name, (PDFS / name).read_bytes(), "application/pdf")})
    assert response.status_code == 201, response.text
    return response.json()


def intake_action(client, intake, action, payload=None, code=200):
    response = client.post(f"/intakes/{intake['id']}/{action}", json=payload)
    assert response.status_code == code, response.text
    return response.json()


def process(client, name, context=None):
    return intake_action(client, upload(client, name, context), "process")


def detail(client, intake):
    response = client.get(f"/intakes/{intake['id']}")
    assert response.status_code == 200, response.text
    return response.json()


def decision(candidate, active, reason="Checked the original document"):
    return {"snapshot_id": candidate["snapshot"]["id"], "active_snapshot_id": active["snapshot"]["id"],
            "reason": reason}


def approval(intake):
    return {"snapshot_id": intake["snapshot"]["id"]}


def test_revisions_replays_equivalence_and_post_order_follow_up(database):
    settings, engine = database
    with TestClient(create_app(settings, fixture_runner)) as client:
        original = process(client, "PO-104-V1.pdf")
        original_snapshot = original["snapshot"]
        assessed = original_snapshot["assessment"]
        assert original["version"]["number"] == 1
        assert not assessed["evidence"]["issues"]
        price = assessed["evidence"]["lines"][0]["unit_price"]
        assert price["status"] == "verified" and price["page"] == 1
        assert "RM90.00" in price["excerpt"]
        assert original["attempt"]["pages"][0]["text"][price["start"]:price["end"]] == price["excerpt"]
        assert assessed["validation"]["lines"][0]["expected_price"] == "100.00"
        assert assessed["validation"]["price_issues"] == ["Line 1 unit price RM90.00 differs from agreed RM100.00."]
        intake_action(client, original, "approve", approval(original), code=409)

        candidate = process(client, "PO-104-V2.pdf")
        assert candidate["version"]["disposition"] == "pending"
        assert candidate["version"]["number"] == 2
        assert candidate["po"]["active_snapshot_id"] == original_snapshot["id"]
        changed = next(row for row in candidate["comparison"]["lines"] if row["status"] == "changed")
        assert [changed[side]["unit_price"] for side in ("active", "candidate")] == ["90.00", "100.00"]
        assert changed["active"]["quantity"] == changed["candidate"]["quantity"] == 2
        rejected = intake_action(client, original, "approve", approval(original), code=409)
        assert any("Pending revisions" in issue for issue in rejected["detail"]["issues"])
        candidate = intake_action(client, candidate, "activate", decision(candidate, original))
        assert candidate["po"]["active_snapshot_id"] == candidate["snapshot"]["id"]
        assert detail(client, original)["version"]["disposition"] == "superseded"
        stale = intake_action(client, original, "approve", approval(original), code=409)
        assert stale["detail"]["refresh"] == f"/intakes/{original['id']}"
        order = intake_action(client, candidate, "approve", approval(candidate))
        assert client.get(f"/orders/{order['id']}").json()["version"] == 2
        assert order["snapshot_id"] == candidate["snapshot"]["id"]
        assert intake_action(client, candidate, "approve", approval(candidate)) == order
        for number, canonical in ((1, original), (2, candidate)):
            replay = upload(client, f"PO-104-V{number}-copy.pdf")
            assert replay["id"] != canonical["id"] and replay["canonical_intake_id"] == canonical["id"]
            assert detail(client, replay)["order"]["id"] == order["id"]

        equivalent = process(client, "PO-104-equivalent.pdf")
        assert equivalent["version"]["disposition"] == "equivalent"
        assert not equivalent["po"]["pending_versions"]
        follow_up = process(client, "PO-104-post-order.pdf")
        assert follow_up["version"]["disposition"] == "post_order"
        assert follow_up["po"]["status"] == "ordered"
        assert follow_up["order"]["snapshot_id"] == order["snapshot_id"]
        intake_action(client, follow_up, "approve", approval(follow_up), code=409)
        booked = client.get(f"/orders/{order['id']}").json()
        assert booked["lines"][0] == {"sku": "BRG-6204", "quantity": 2, "unit_price": "100.00"}
        assert len(client.get("/orders").json()) == 1
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(Attempt)) == 4
        assert db.scalar(select(func.count()).select_from(Intake)) == 6
        assert db.scalar(select(func.count()).select_from(Version)) == 4


def test_staff_corrects_misread_without_changing_quoted_business_price(database):
    settings, engine = database
    with TestClient(create_app(settings, extract_demo)) as client:
        before = process(client, "PO-105-misread.pdf")
        assessment = before["snapshot"]["assessment"]
        finding = assessment["evidence"]["lines"][0]["unit_price"]
        assert finding["value"] == "RM100.00" and finding["status"] == "unverified"
        assert "excerpt" not in finding
        assert not assessment["validation"]["price_issues"]
        assert client.get("/orders").json() == []

        quote = "BRG-6204 Deep groove bearing 2 EA RM90.00"
        patch = {**decision(before, before), "field": "unit_price", "line": 1, "page": 1,
                 "excerpt": quote, "value": "RM100.00"}
        intake_action(client, before, "correct", patch, code=422)
        patch["value"] = "RM90.00"
        after = intake_action(client, before, "correct", patch)
        assert after["snapshot"]["edit"] == 2
        assert after["snapshot"]["parent_id"] == before["snapshot"]["id"]
        assert after["attempt"]["draft"] == before["attempt"]["draft"]
        assert after["history"][0]["edits"][0]["draft"] == before["snapshot"]["draft"]
        price = after["snapshot"]["assessment"]["evidence"]["lines"][0]["unit_price"]
        assert price["status"] == "verified" and price["value"] == "RM90.00"
        assert after["snapshot"]["assessment"]["validation"]["price_issues"]
        intake_action(client, before, "correct", patch, code=409)
        intake_action(client, after, "approve", approval(after), code=409)
        assert any(item["action"] == "approval_blocked" for item in detail(client, after)["activity"])
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(Snapshot)) == 2
        assert db.scalar(select(func.count()).select_from(SalesOrder)) == 0


@pytest.mark.parametrize(("value", "excerpt", "page", "issue"), [
    ("RM100.00", "BRG-6204 Deep groove bearing 2 EA RM100.00 (agreed)", 1, "absent"),
    ("RM100.00", "BRG-6204 Deep groove bearing 2 EA RM100.00", 2, "valid source page"),
    ("RM45.50", "BELT-A42 Industrial V-belt 3 EA RM45.50", 1, "correct SKU row"),
])
def test_schema_valid_unsupported_evidence_blocks_approval(database, value, excerpt, page, issue):
    settings, _ = database

    def unsupported(_, pages):
        data = fixture_draft(pages)
        data["lines"][0]["unit_price"] = proposal(value, excerpt, page)
        return json.dumps(data)

    with TestClient(create_app(settings, unsupported)) as client:
        intake = process(client, "PO-103.pdf")
        finding = intake["snapshot"]["assessment"]["evidence"]["lines"][0]["unit_price"]
        assert issue in finding["issue"] and "excerpt" not in finding
        intake_action(client, intake, "approve", approval(intake), code=409)
        assert not client.get("/orders").json()


def test_total_column_and_repeated_sku_association():
    pages = [{"page": 1, "text": (
        "PO number: PO-008\nCustomer reference: CUST-101\nCurrency: MYR\n"
        "SKU  Qty  Unit price  Line total\nBRG-6204  2  RM100.00  RM200.00\n"
        "BRG-6204  3  RM100.00  RM300.00\n"
    )}]
    one = "BRG-6204 2 RM100.00 RM200.00"
    two = "BRG-6204 3 RM100.00 RM300.00"
    draft = Draft.model_validate({
        "po_number": proposal("PO-008", "PO number:\n PO-008"),
        "customer_reference": proposal("CUST-101", "Customer reference: CUST-101"),
        "currency": proposal("MYR", "Currency: MYR"),
        "lines": [
            {"sku": proposal("BRG-6204", one), "quantity": proposal("2", one),
             "unit_price": proposal("RM200.00", one)},
            {"sku": proposal("BRG-6204", two), "quantity": proposal("3", two),
             "unit_price": proposal("RM100.00", "RM100.00")},
        ],
    })
    evidence = verify(draft, pages)
    assert evidence["fields"]["po_number"]["status"] == "verified"
    assert "correct SKU row and column" in evidence["lines"][0]["unit_price"]["issue"]
    assert "repeats" in evidence["lines"][1]["unit_price"]["issue"]
    draft.lines[0].unit_price.value = "RM100.00"
    draft.lines[1].unit_price.excerpt = two
    assert not verify(draft, pages)["issues"]
    draft.lines[0].quantity.value, draft.lines[0].quantity.excerpt = "3", two
    assert any("different PDF rows" in item for item in verify(draft, pages)["issues"])
    old = {"draft": draft.model_dump(), "assessment": None}
    new = {"draft": draft.model_dump(), "assessment": None}
    assert [row["status"] for row in diff_lines(old, new)] == ["unmatched"] * 4


def test_ambiguous_identity_stays_unlinked_until_supported_correction(database):
    settings, engine = database
    with TestClient(create_app(settings, fixture_runner)) as client:
        first = process(client, "PO-104-V1.pdf")

    def ambiguous(_, pages):
        data = fixture_draft(pages)
        data["po_number"] = proposal(None, None, alternatives=["PO-103", "PO-104"])
        return json.dumps(data)

    with TestClient(create_app(settings, ambiguous)) as client:
        unclear = process(client, "PO-104-V2.pdf")
        assert unclear["po"] is None
        assert unclear["identity_options"]["po_numbers"] == ["PO-103", "PO-104"]
        assert unclear["identity_options"]["orders"] == [{"id": first["po"]["id"], "po_key": "po-104"}]
        intake_action(client, unclear, "approve", approval(unclear), code=409)
        patch = {"snapshot_id": unclear["snapshot"]["id"], "active_snapshot_id": None, "reason": "Read header",
                 "field": "po_number", "page": 1, "excerpt": "PO number: PO-104", "value": "PO-103"}
        intake_action(client, unclear, "correct", patch, code=422)
        resolved = intake_action(client, unclear, "correct", {**patch, "value": "PO-104"})
        assert resolved["po"]["id"] == first["po"]["id"]
        assert resolved["version"]["disposition"] == "pending"
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(PurchaseOrder)) == 1


def test_conflicting_context_does_not_expose_order_until_evidenced_resolution(database):
    settings, _ = database
    with TestClient(create_app(settings, fixture_runner)) as client:
        original = process(client, "PO-104-V2.pdf")
        assert original["po"]["status"] == "ready_for_approval"
        intake_action(client, original, "approve", approval(original))
        conflicting = process(client, "PO-104-V2-copy.pdf", "CUST-102")
        assert conflicting["canonical_intake_id"] is None
        assert conflicting["order"] is None and conflicting["po"] is None
        assert conflicting["identity_options"]["orders"] == []
        assert conflicting["snapshot"]["assessment"]["validation"]["lines"][0]["expected_price"] is None
        assert any("context differs" in issue for issue in conflicting["snapshot"]["assessment"]["issues"])
        patch = {"snapshot_id": conflicting["snapshot"]["id"], "active_snapshot_id": None,
                 "reason": "The selected customer was incorrect", "field": "customer_reference", "page": 1,
                 "excerpt": "Customer reference: CUST-101", "value": "CUST-101", "confirm_customer": True}
        resolved = intake_action(client, conflicting, "correct", patch)
        assert resolved["canonical_intake_id"] == original["id"]
        assert resolved["customer_context"] == "CUST-102"
        assert resolved["receipt_edit_history"][-1]["edit"] == 2
        assert len(client.get("/orders").json()) == 1


def test_concurrent_exact_uploads_share_pending_work(database):
    settings, engine = database
    with TestClient(create_app(settings, fixture_runner)) as client:
        gate = Barrier(2)

        def submit(name):
            gate.wait(timeout=10)
            return upload(client, name)

        with ThreadPoolExecutor(max_workers=2) as threads:
            receipts = list(threads.map(submit, ("PO-104-V2.pdf", "PO-104-V2-copy.pdf")))
        canonical = next(item for item in receipts if item["canonical_intake_id"] is None)
        replay = next(item for item in receipts if item != canonical)
        assert replay["canonical_intake_id"] == canonical["id"]
        result = intake_action(client, replay, "process")
        assert result["attempt"]["id"] == detail(client, canonical)["attempt"]["id"]
        assert result["state"] == "complete"
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(Attempt)) == 1


def test_activate_racing_old_approval_uses_same_po_lock(database):
    settings, engine = database
    with (TestClient(create_app(settings, fixture_runner)) as staff_a,
          TestClient(create_app(settings, fixture_runner)) as staff_b):
        first = process(staff_a, "PO-104-V2.pdf")
        candidate = process(staff_b, "PO-104-post-order.pdf")
        assert first["po"]["status"] == "ready_for_approval"
        assert candidate["po"]["pending_versions"] == [2]
        gate = Barrier(2)

        def activate():
            gate.wait(timeout=10)
            return intake_action(staff_b, candidate, "activate", decision(candidate, first))

        def approve_old():
            gate.wait(timeout=10)
            return intake_action(staff_a, first, "approve", approval(first), code=409)

        with ThreadPoolExecutor(max_workers=2) as threads:
            switched = threads.submit(activate)
            rejected = threads.submit(approve_old)
            active, blocked = switched.result(timeout=10), rejected.result(timeout=10)
        assert "stale" in blocked["detail"]["message"] or "Pending revisions" in blocked["detail"]["issues"][0]
        assert staff_b.get("/orders").json() == []
        order = intake_action(staff_b, active, "approve", approval(active))
        assert order["snapshot_id"] == active["snapshot"]["id"]
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(SalesOrder)) == 1


def test_dismissal_and_pending_equivalent_rechecked_when_active_changes(database):
    settings, _ = database
    with TestClient(create_app(settings, fixture_runner)) as client:
        first = process(client, "PO-104-V1.pdf")
        replacement = process(client, "PO-104-V2.pdf")
        equivalent = process(client, "PO-104-equivalent.pdf")
        separate = process(client, "PO-104-post-order.pdf")
        assert separate["po"]["pending_versions"] == [2, 3, 4]
        active = intake_action(client, replacement, "activate", decision(replacement, first))
        assert detail(client, equivalent)["version"]["disposition"] == "equivalent"
        assert active["po"]["pending_versions"] == [4]
        intake_action(client, separate, "dismiss", decision(separate, first), code=409)
        dismissed = intake_action(client, separate, "dismiss", decision(separate, active, "Customer withdrew revision"))
        assert dismissed["version"]["disposition"] == "dismissed"
        assert dismissed["po"]["pending_versions"] == []
        assert dismissed["po"]["status"] == "ready_for_approval"
        assert any(event["detail"].get("reason") == "Customer withdrew revision" for event in dismissed["activity"])


def test_unresolved_equivalence_requires_review_even_after_approval(database):
    settings, _ = database
    with TestClient(create_app(settings, fixture_runner)) as client:
        active = process(client, "PO-104-V2.pdf")
        order = intake_action(client, active, "approve", approval(active))

    def unverified(_, pages):
        draft = fixture_draft(pages)
        draft["lines"][0]["unit_price"]["excerpt"] += " copied from the quote"
        return json.dumps(draft)

    with TestClient(create_app(settings, unverified)) as client:
        unresolved = process(client, "PO-104-equivalent.pdf")
        assert unresolved["version"]["disposition"] == "post_order"
        corrected = intake_action(client, unresolved, "correct", {
            **decision(unresolved, active), "field": "unit_price", "line": 1, "page": 1,
            "excerpt": "BELT-A42 Industrial V-belt 3 EA RM45.5", "value": "RM45.5",
        })
        assert corrected["version"]["disposition"] == "equivalent"
        assert corrected["po"]["active_snapshot_id"] == order["snapshot_id"]
        assert len(client.get("/orders").json()) == 1


def test_extraction_finishing_after_approval_creates_follow_up(database):
    settings, _ = database
    running, publish = Event(), Event()

    def delayed(_, pages):
        running.set()
        assert publish.wait(timeout=10)
        return fixture_runner(None, pages)

    with (TestClient(create_app(settings, fixture_runner)) as staff,
          TestClient(create_app(settings, delayed)) as intake_client,
          ThreadPoolExecutor(max_workers=1) as worker):
        active = process(staff, "PO-104-V2.pdf")
        receipt = upload(intake_client, "PO-104-post-order.pdf")
        later = worker.submit(intake_action, intake_client, receipt, "process")
        try:
            assert running.wait(timeout=10)
            order = intake_action(staff, active, "approve", approval(active))
        finally:
            publish.set()
        follow_up = later.result(timeout=10)
        assert follow_up["version"]["disposition"] == "post_order"
        assert follow_up["order"] == order


def test_migration_preserves_milestone1_order_and_rechecks_open_draft(postgres_schema):
    settings, engine = postgres_schema
    command.upgrade(Config("alembic.ini"), "0001")
    seed(engine)
    with engine.begin() as db:
        for name, intake_id in (("PO-103.pdf", 1), ("PO-104-V2.pdf", 2)):
            pdf = (PDFS / name).read_bytes()
            digest, path = store_pdf(settings.storage, pdf)
            pages = pdf_pages(pdf)
            data = fixture_draft(pages)
            db.execute(text("INSERT INTO documents (digest,path,size) VALUES (:digest,:path,:size)"),
                       {"digest": digest, "path": path, "size": len(pdf)})
            db.execute(text("INSERT INTO intakes (id,digest,filename,state) VALUES (:id,:digest,:name,'complete')"),
                       {"id": intake_id, "digest": digest, "name": name})
            db.execute(text("INSERT INTO attempts (intake_id,state,settings,pages,draft) "
                            "VALUES (:id,'complete','{}',CAST(:pages AS JSON),CAST(:draft AS JSON))"),
                       {"id": intake_id, "pages": json.dumps(pages), "draft": json.dumps(data)})
        db.execute(text("INSERT INTO sales_orders (id,customer,po_key,po_number,currency,intake_id,snapshot_id,reviewer) "
                        "VALUES (9,'CUST-101','po-103','PO-103','MYR',1,1,'Original Reviewer')"))
        db.execute(text("INSERT INTO order_lines (order_id,position,sku,quantity,unit_price) "
                        "VALUES (9,1,'BRG-6204',2,100.00)"))
    command.upgrade(Config("alembic.ini"), "head")
    command.check(Config("alembic.ini"))
    with TestClient(create_app(settings, fixture_runner)) as client:
        old = detail(client, {"id": 1})
        assert old["snapshot"]["id"] == 1
        assert old["order"]["reviewer"] == "Original Reviewer"
        assert old["order"]["number"] == "SO-000009"
        assert intake_action(client, old, "approve", approval(old)) == old["order"]
        order = client.get("/orders/9").json()
        assert order["lines"] == [{"quantity": 2, "sku": "BRG-6204", "unit_price": "100.00"}]
        assert client.get("/intakes/1/pdf").content == (PDFS / "PO-103.pdf").read_bytes()
        open_draft = detail(client, {"id": 2})
        assert open_draft["po"] is None
        checked = intake_action(client, open_draft, "validate", approval(open_draft))
        assert checked["po"]["status"] == "ready_for_approval"
        assert not checked["snapshot"]["assessment"]["issues"]
