import base64
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Barrier, Event, Thread

import httpx
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from po_intake import jobs, notifications, worker
from po_intake.api import create_app
from po_intake.db import Attempt, Document, ExtractionJob, Intake, Notification, NotificationAttempt, SalesOrder, Version
from po_intake.extraction import TransientExtractionError
from scripts.milestone2_demo import extract_demo

from test_milestone2 import PDFS, approval, decision, detail, fixture_runner, intake_action, upload


KEY = "credential-free-integration-key"
HEADERS = {"X-Integration-Key": KEY}


@pytest.fixture
def durable(database):
    settings, engine = database
    return replace(
        settings, foreground=False, integration_key=KEY, outbound_enabled=True,
        telegram_chat="-10012345678", gmail_senders={"buyer@example.test": "CUST-101",
                                                     "other@example.test": "CUST-102"},
        gmail_recipients={"CUST-101": "recipient@example.test"},
        extraction_backoff=0, notification_backoff=0,
    ), engine


def gmail(client, filename, message, attachment="att_1", sender="Demo Buyer <buyer@example.test>"):
    response = client.post("/integrations/gmail/intakes", headers=HEADERS, json={
        "message_id": message, "attachment_id": attachment, "sender": sender, "filename": filename,
        "pdf_base64": base64.urlsafe_b64encode((PDFS / filename).read_bytes()).decode().rstrip("="),
    })
    assert response.status_code in {200, 201}, response.text
    return response.json()


def claim(client):
    response = client.post("/integrations/notifications/claim", headers=HEADERS)
    assert response.status_code == 200, response.text
    return response.json()["delivery"]


def callback(client, delivery, outcome, code=200, **fields):
    response = client.post(f"/integrations/notifications/attempts/{delivery['attempt_id']}/result",
                           headers=HEADERS, json={"outcome": outcome, **fields})
    assert response.status_code == code, response.text
    return response.json()


def states(client, intake):
    response = client.get(f"/intakes/{intake['id']}/notifications")
    assert response.status_code == 200, response.text
    return response.json()


def test_gmail_replays_and_exact_resends_share_one_job(durable):
    settings, engine = durable
    with TestClient(create_app(settings)) as client:
        gate = Barrier(2)

        def submit(_):
            gate.wait(timeout=10)
            return gmail(client, "PO-104-V1.pdf", "message1")

        with ThreadPoolExecutor(max_workers=2) as callers:
            receipts = list(callers.map(submit, range(2)))
        first = receipts[0]
        assert first["id"] == receipts[1]["id"]
        assert sorted(row["replayed"] for row in receipts) == [False, True]
        assert first["source"] == "gmail" and first["sender"] == "buyer@example.test"
        replay = gmail(client, "PO-104-V2.pdf", "message1")
        assert replay["id"] == first["id"] and replay["replayed"]
        second = gmail(client, "PO-104-V1-copy.pdf", "message1", attachment="att_2")
        third = gmail(client, "PO-104-V1-copy.pdf", "message2")
        local = upload(client, "PO-104-V1-copy.pdf", context="CUST-101")
        for row in (second, third, local):
            assert row["id"] != first["id"] and row["canonical_intake_id"] == first["id"]
        assert client.get(f"/intakes/{first['id']}/pdf").content == (PDFS / "PO-104-V1.pdf").read_bytes()
        assert worker.run_one(engine, settings, extract_demo)
        assert worker.run_one(engine, settings, extract_demo) is None
        assert detail(client, third)["attempt"]["id"] == detail(client, first)["attempt"]["id"]
        assert len(states(client, first)) == 1

    with Session(engine) as db:
        for model, expected in ((Intake, 4), (Document, 1), (ExtractionJob, 1), (Attempt, 1), (Version, 1)):
            assert db.scalar(select(func.count()).select_from(model)) == expected


@pytest.mark.parametrize("sender", ["Unmapped <nobody@example.test>", "Other <other@example.test>"])
def test_gmail_unmapped_or_conflicting_sender_stays_unlinked(durable, sender):
    settings, engine = durable
    with TestClient(create_app(settings)) as client:
        original = gmail(client, "PO-104-V2.pdf", "good")
        assert worker.run_one(engine, settings, extract_demo)
        intake_action(client, original, "approve", approval(detail(client, original)))
        bad = gmail(client, "PO-104-V2.pdf", "bad", sender=sender)
        assert bad["canonical_intake_id"] is None
        assert worker.run_one(engine, settings, extract_demo)
        bad = detail(client, bad)
        assert bad["po"] is None and bad["order"] is None
        assert bad["snapshot"]["assessment"]["validation"]["lines"][0]["expected_price"] is None
        assert bad["snapshot"]["assessment"]["issues"]
        intake_action(client, bad, "correct", {
            "snapshot_id": bad["snapshot"]["id"], "active_snapshot_id": None, "field": "customer_reference",
            "value": "CUST-101", "page": 1, "excerpt": "Customer reference: CUST-101", "reason": "Looked at PDF",
            "confirm_customer": True,
        }, code=422)
        assert len(client.get("/orders").json()) == 1


def test_worker_kill_recovers_and_fences_stale_results(durable):
    settings, engine = durable
    called, release = Event(), Event()

    class SlowOllama(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            called.set()
            release.wait(timeout=15)

    with ThreadingHTTPServer(("127.0.0.1", 0), SlowOllama) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        env = {**os.environ, "DATABASE_URL": settings.database_url, "PDF_STORAGE": str(settings.storage),
               "OLLAMA_URL": f"http://127.0.0.1:{server.server_port}", "PO_EXTRACTION_LEASE_SECONDS": "90"}
        try:
            with TestClient(create_app(settings)) as client:
                receipt = gmail(client, "PO-104-V1.pdf", "worker_kill")
                with subprocess.Popen([sys.executable, "-m", "po_intake.worker", "--once"], env=env) as process:
                    try:
                        assert called.wait(timeout=12), process.poll()
                        with Session(engine) as db:
                            job = db.get(ExtractionJob, receipt["id"])
                            assert job.state == "running"
                            document = db.get(Document, db.get(Intake, job.intake_id).digest)
                            previous = jobs.Claim(job.intake_id, job.attempt_id, job.claim_token,
                                                  settings.storage / document.path)
                    finally:
                        if process.poll() is None:
                            if os.name == "nt":
                                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                               capture_output=True, check=True, timeout=10)
                            else:
                                process.kill()
                        process.wait(timeout=10)
                assert detail(client, receipt)["state"] == "processing"
                assert jobs.claim(engine, settings) is None
                with Session(engine) as db, db.begin():
                    db.get(ExtractionJob, receipt["id"]).lease_until = notifications.clock(db) - timedelta(seconds=1)
                replacement = jobs.claim(engine, settings)
                assert replacement.token != previous.token and replacement.attempt_id != previous.attempt_id
                assert replacement.path == previous.path
                results = worker.extract(settings, previous.path, extract_demo)
                assert not worker.publish(engine, settings, previous, results)
                assert worker.publish(engine, settings, replacement, results)
                assert detail(client, receipt)["job"]["state"] == "complete"
                assert detail(client, receipt)["attempt"]["id"] == replacement.attempt_id
                with Session(engine) as db:
                    attempts = db.scalars(select(Attempt).order_by(Attempt.id)).all()
                    assert [attempt.state for attempt in attempts] == ["failed", "complete"]
                    assert "lease expired" in attempts[0].error
                    assert db.scalar(select(func.count()).select_from(Version)) == 1
        finally:
            release.set()
            server.shutdown()
            thread.join(timeout=5)


def test_worker_retries_transient_failure_within_budget_and_staff_can_requeue(durable):
    settings, engine = durable
    settings = replace(settings, extraction_max_attempts=2)

    def unavailable(*_):
        raise TransientExtractionError("Demo runner temporarily unavailable.")

    with TestClient(create_app(settings)) as client:
        row = upload(client, "PO-103.pdf")
        assert worker.run_one(engine, settings, unavailable)
        retry = detail(client, row)
        assert retry["state"] == "pending" and retry["attempt"]["state"] == "failed"
        assert states(client, row) == []
        assert worker.run_one(engine, settings, unavailable)
        blocked = detail(client, row)
        assert blocked["state"] == "failed" and blocked["job"]["attempt_count"] == 2
        assert worker.run_one(engine, settings, unavailable) is None
        assert len(states(client, row)) == 1
        reopened = intake_action(client, row, "process")
        assert reopened["state"] == "pending"
        assert worker.run_one(engine, settings, fixture_runner)
        assert detail(client, row)["state"] == "complete"
        assert states(client, row)[0]["closed_at"]
        assert claim(client) is None


def test_expired_worker_lease_exhausts_its_last_attempt(durable):
    settings, engine = durable
    settings = replace(settings, extraction_max_attempts=1)
    with TestClient(create_app(settings)) as client:
        receipt = upload(client, "PO-103.pdf")
        assert jobs.claim(engine, settings)
        with Session(engine) as db, db.begin():
            db.get(ExtractionJob, receipt["id"]).lease_until = notifications.clock(db) - timedelta(seconds=1)
        assert jobs.claim(engine, settings) is None
        assert jobs.claim(engine, settings) is None
        view = detail(client, receipt)
        assert view["state"] == view["job"]["state"] == "failed"
        assert view["job"]["attempt_count"] == 1
        assert "lease expired" in view["attempt"]["error"]
        assert len(states(client, receipt)) == 1


class MockProvider:
    def __init__(self):
        self.accepted = []

    def send(self, delivery, lose_response=False):
        self.accepted.append((delivery["channel"], delivery["destination"], delivery["payload"]))
        if lose_response:
            raise httpx.ReadTimeout("Mock provider accepted, but its response was lost.")
        return f"provider-{len(self.accepted)}"


def test_end_to_end_po104_outbox_claims_dupe_callback_and_lost_ack(durable):
    settings, engine = durable
    provider = MockProvider()
    with TestClient(create_app(settings)) as client:
        original = gmail(client, "PO-104-V1.pdf", "v1")
        assert worker.run_one(engine, settings, extract_demo)
        original = detail(client, original)
        assert original["po"]["status"] == "needs_review"
        assert not original["snapshot"]["assessment"]["evidence"]["issues"]
        assert original["snapshot"]["assessment"]["validation"]["price_issues"]
        intake_action(client, original, "validate", approval(original))
        assert len(states(client, original)) == 1
        gate = Barrier(2)

        def poll(_):
            gate.wait(timeout=10)
            return claim(client)

        with ThreadPoolExecutor(max_workers=2) as callers:
            claimed = list(callers.map(poll, range(2)))
        assert sum(delivery is not None for delivery in claimed) == 1
        alert = next(item for item in claimed if item)
        assert "PO-104" in alert["payload"]["text"] and "RM90.00" in alert["payload"]["text"]
        assert f"?intake={original['id']}" in alert["payload"]["text"]
        alert_receipt = provider.send(alert)
        assert callback(client, alert, "sent", provider_receipt=alert_receipt) == {"state": "sent"}
        assert callback(client, alert, "sent", provider_receipt=alert_receipt) == {"state": "sent"}
        callback(client, alert, "unknown", code=409)

        candidate = gmail(client, "PO-104-V2.pdf", "v2")
        assert worker.run_one(engine, settings, extract_demo)
        candidate = detail(client, candidate)
        assert candidate["version"]["disposition"] == "pending"
        assert len(states(client, original)) == 1
        active = intake_action(client, candidate, "activate", decision(candidate, original))
        order = intake_action(client, active, "approve", approval(active))
        assert intake_action(client, active, "approve", approval(active)) == order
        same_event = gmail(client, "PO-104-V2.pdf", "v2")
        resend = gmail(client, "PO-104-V2-copy.pdf", "v2_resend")
        assert same_event["id"] == active["id"] and resend["canonical_intake_id"] == active["id"]
        equivalent = gmail(client, "PO-104-equivalent.pdf", "equivalent")
        assert worker.run_one(engine, settings, extract_demo)
        assert detail(client, equivalent)["version"]["disposition"] == "equivalent"
        assert len(states(client, active)) == 2

        mail = claim(client)
        assert mail["channel"] == "gmail" and mail["destination"] == "recipient@example.test"
        assert "PO-104" in mail["payload"]["text"] and order["number"] in mail["payload"]["text"]
        with pytest.raises(httpx.ReadTimeout):
            provider.send(mail, lose_response=True)
        with Session(engine) as db, db.begin():
            db.get(NotificationAttempt, mail["attempt_id"]).deadline = notifications.clock(db) - timedelta(seconds=1)
        assert claim(client) is None
        unknown = states(client, active)[1]
        assert unknown["state"] == "unknown" and unknown["attempts"][0]["outcome"] == "unknown"
        assert detail(client, active)["po"]["status"] == "ordered"
        callback(client, mail, "sent", code=409, provider_receipt="late-provider-id")
        assert client.post(f"/notifications/{unknown['id']}/retry", json={"reason": "Too soon"}).status_code == 409
        response = client.post(f"/notifications/{unknown['id']}/reconcile", json={
            "outcome": "delivered", "reason": "Found matching mail in the test inbox.", "provider_receipt": "found-in-inbox",
        })
        assert response.json() == {"state": "sent"}
        assert states(client, active)[1]["attempts"][0]["reconciliation"]["outcome"] == "delivered"
        assert claim(client) is None
        assert [accepted[0] for accepted in provider.accepted] == ["telegram", "gmail"]
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(SalesOrder)) == 1
        assert db.scalar(select(func.count()).select_from(Notification)) == 2
        assert db.scalar(select(func.count()).select_from(NotificationAttempt)) == 2


def test_known_non_delivery_has_bounded_retries_and_rejects_superseded_callbacks(durable):
    settings, engine = durable
    settings = replace(settings, notification_max_attempts=2)
    with TestClient(create_app(settings)) as client:
        receipt = gmail(client, "PO-104-V2.pdf", "known_failure")
        assert worker.run_one(engine, settings, extract_demo)
        ready = detail(client, receipt)
        intake_action(client, ready, "approve", approval(ready))
        first = claim(client)
        result = {"retryable": True, "error": "Provider explicitly rejected the send (429)."}
        callback(client, first, "failed", **result)
        assert callback(client, first, "failed", **result) == {"state": "failed"}
        second = claim(client)
        assert second["notification_id"] == first["notification_id"] and second["attempt_id"] != first["attempt_id"]
        callback(client, first, "failed", code=409, **result)
        callback(client, second, "failed", **result)
        assert claim(client) is None
        assert len(states(client, ready)[0]["attempts"]) == 2


def test_unknown_requires_reconciliation_and_a_separate_resend_authorization(durable):
    settings, engine = durable
    with TestClient(create_app(settings)) as client:
        receipt = gmail(client, "PO-104-V2.pdf", "reconcile_non_delivery")
        assert worker.run_one(engine, settings, extract_demo)
        ready = detail(client, receipt)
        intake_action(client, ready, "approve", approval(ready))
        uncertain = claim(client)
        callback(client, uncertain, "unknown", error="No response from provider.")
        path = f"/notifications/{uncertain['notification_id']}"
        assert client.post(f"{path}/retry", json={"reason": "Don't know yet"}).status_code == 409
        assert client.post(f"{path}/reconcile", json={
            "outcome": "not_delivered", "reason": "Checked Gmail Sent and the test receiver.",
        }).json() == {"state": "failed"}
        assert claim(client) is None
        assert client.post(f"{path}/retry", json={"reason": "Authorize one resend."}).json() == {"state": "pending"}
        again = claim(client)
        assert again["notification_id"] == uncertain["notification_id"]
        assert again["attempt_id"] != uncertain["attempt_id"]
        callback(client, again, "sent", provider_receipt="resent-once")
        assert len(client.get("/orders").json()) == 1
        assert claim(client) is None


def test_integrations_require_key_and_delivery_defaults_off(durable):
    settings, engine = durable
    disabled = replace(settings, outbound_enabled=False, staff_key="separate-staff-key")
    with TestClient(create_app(disabled)) as client:
        assert client.post("/integrations/notifications/claim").status_code == 401
        assert client.post("/integrations/gmail/intakes", json={}).status_code == 401
        assert client.get("/intakes", headers=HEADERS).status_code == 401
        assert client.get("/intakes", headers={"X-Staff-Key": "separate-staff-key"}).status_code == 200
        gmail(client, "PO-104-V1.pdf", "disabled")
        assert worker.run_one(engine, disabled, extract_demo)
        assert claim(client) is None
    with TestClient(create_app(replace(settings, telegram_chat="987654321"))) as client:
        assert claim(client) is None
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(Notification)) == 1
        assert db.scalar(select(func.count()).select_from(NotificationAttempt)) == 0
    with TestClient(create_app(settings)) as client:
        assert claim(client)["channel"] == "telegram"


def test_migration_requeues_legacy_pending_and_interrupted_receipts(postgres_schema):
    settings, engine = postgres_schema
    command.upgrade(Config("alembic.ini"), "0002")
    with engine.begin() as db:
        db.execute(text("INSERT INTO documents(digest,path,size) VALUES ('fixture','fixture.pdf',1)"))
        db.execute(text("INSERT INTO intakes(digest,filename,state) VALUES "
                        "('fixture','pending.pdf','pending'), ('fixture','interrupted.pdf','processing')"))
        db.execute(text("INSERT INTO intakes(digest,filename,state,canonical_id) "
                        "VALUES ('fixture','resend.pdf','pending',1)"))
        db.execute(text("INSERT INTO attempts(intake_id,state,settings) VALUES (2,'processing','{}')"))
    command.upgrade(Config("alembic.ini"), "head")
    command.check(Config("alembic.ini"))
    with Session(engine) as db:
        assert db.scalars(select(ExtractionJob.intake_id).order_by(ExtractionJob.intake_id)).all() == [1, 2]
        assert db.get(Intake, 2).state == "pending"
        assert db.get(Attempt, 1).state == "failed"
        assert "migration" in db.get(Attempt, 1).error
