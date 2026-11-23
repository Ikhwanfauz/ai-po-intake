import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier, Event
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from po_intake.api import create_app
from po_intake.config import Settings
from po_intake.db import Attempt, Document, Intake, OrderLine, Price, SalesOrder, make_engine
from po_intake.extraction import extract_with_ollama
from po_intake.seed import seed


PDF = Path("output/pdf/PO-103.pdf").read_bytes()


@pytest.fixture
def database(tmp_path, monkeypatch):
    db_name = "test_" + uuid4().hex
    admin = make_engine(Settings().database_url)
    with admin.begin() as connection:
        connection.execute(text(f"CREATE SCHEMA {db_name}"))
    url = make_url(Settings().database_url).update_query_dict({"options": f"-csearch_path={db_name}"})
    monkeypatch.setenv("DATABASE_URL", url.render_as_string(hide_password=False))
    settings = replace(Settings(), storage=tmp_path)
    engine = make_engine(settings.database_url)
    try:
        command.upgrade(Config("alembic.ini"), "head")
        seed(engine)
        yield settings, engine
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f"DROP SCHEMA {db_name} CASCADE"))
        admin.dispose()


def proposal(value):
    return {"value": value, "page": 1, "excerpt": value}


def draft(po="PO-103", price="RM100.00", unit="EA"):
    return json.dumps({
        "po_number": proposal(po),
        "customer_reference": proposal("CUST-101"),
        "currency": proposal("MYR"),
        "lines": [{
            "sku": proposal("BRG-6204"), "quantity": proposal("2"),
            "unit_price": proposal(price), "unit": unit,
        }],
    })


def upload(client):
    response = client.post("/intakes", files={"file": ("PO-103.pdf", PDF, "application/pdf")})
    assert response.status_code == 201, response.text
    assert response.json()["state"] == "pending"
    return response.json()["id"]


def process(client, intake_id):
    response = client.post(f"/intakes/{intake_id}/process")
    assert response.status_code == 200, response.text
    return response.json()


def approve(client, intake_id, snapshot_id):
    return client.post(f"/intakes/{intake_id}/approve", json={"snapshot_id": snapshot_id})


def test_durable_failure_restart_retry_and_order(database):
    settings, engine = database
    calls = []

    def successful_runner(_, pages):
        calls.append(pages)
        assert pages[0]["page"] == 1
        assert "CUST-101" in pages[0]["text"]
        return draft()

    broken_runner = replace(settings, ollama_url="http://127.0.0.1:1", timeout=1)
    with TestClient(create_app(broken_runner, extract_with_ollama)) as client:
        intake = upload(client)
        failed = process(client, intake)
        assert failed["state"] == "failed"
        assert "Ollama request failed" in failed["attempt"]["error"]

    with TestClient(create_app(settings, successful_runner)) as client:
        assert client.get(f"/intakes/{intake}").json() == failed
        assert client.get(f"/intakes/{intake}/pdf").content == PDF
        assert client.get(f"/intakes/{intake}/pages/1").content.startswith(b"\x89PNG")
        complete = process(client, intake)
        snapshot = complete["attempt"]["id"]
        assert complete["state"] == "complete"
        assert complete["attempt"]["validation"]["issues"] == []
        assert snapshot != failed["attempt"]["id"]
        assert approve(client, intake, failed["attempt"]["id"]).status_code == 409
        result = approve(client, intake, snapshot)
        assert result.status_code == 200, result.text
        assert approve(client, intake, snapshot).json() == result.json()

    with TestClient(create_app(settings, successful_runner)) as client:
        order = client.get(f"/orders/{result.json()['id']}").json()
        assert order["snapshot_id"] == snapshot
        assert order["lines"] == [{"sku": "BRG-6204", "quantity": 2, "unit_price": "100.00"}]
        assert len(calls) == 1

    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(Intake)) == 1
        assert db.scalar(select(func.count()).select_from(Document)) == 1
        assert db.scalars(select(Attempt.state).order_by(Attempt.id)).all() == ["failed", "complete"]


@pytest.mark.parametrize(("po", "price", "unit", "issue"), [
    (None, None, "EA", "PO number"),
    ("PO-103", "RM90.00", "EA", "differs from agreed RM100.00"),
    ("PO-103", "RM100.001", "EA", "at most two decimals"),
    ("PO-103", "RM100.00", "BOX", "unsupported unit"),
])
def test_blocking_drafts(database, po, price, unit, issue):
    settings, _ = database
    with TestClient(create_app(settings, lambda *_: draft(po, price, unit))) as client:
        intake = upload(client)
        snapshot = process(client, intake)["attempt"]
        assert any(issue in item for item in snapshot["validation"]["issues"])
        assert approve(client, intake, snapshot["id"]).status_code == 409
        assert client.get("/orders").json() == []


def test_approval_rechecks_current_price(database):
    settings, engine = database
    with TestClient(create_app(settings, lambda *_: draft())) as client:
        intake = upload(client)
        snapshot = process(client, intake)["attempt"]
        with Session(engine) as db, db.begin():
            db.get(Price, ("CUST-101", "BRG-6204", "MYR")).amount = 101
        assert approve(client, intake, snapshot["id"]).status_code == 409
        updated = client.get(f"/intakes/{intake}").json()["attempt"]["validation"]
        assert updated["lines"][0]["expected_price"] == "101.00"
        assert client.get("/orders").json() == []


@pytest.mark.parametrize("second_receipt", [False, True], ids=["same-snapshot", "same-business-key"])
def test_concurrent_approvals_make_one_order(database, second_receipt):
    settings, engine = database
    with TestClient(create_app(settings, lambda *_: draft())) as client:
        first = upload(client)
        second = upload(client) if second_receipt else first
        snapshots = {i: process(client, i)["attempt"]["id"] for i in {first, second}}
        gate = Barrier(2)

        def submit(intake):
            gate.wait(timeout=10)
            return approve(client, intake, snapshots[intake])

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(submit, (first, second)))
        assert sorted(r.status_code for r in results) == ([200, 409] if second_receipt else [200, 200])
        assert len({r.json()["id"] for r in results if r.status_code == 200}) == 1
        assert len(client.get("/orders").json()) == 1

    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(SalesOrder)) == 1
        assert db.scalar(select(func.count()).select_from(OrderLine)) == 1


def test_interrupted_attempt_cannot_publish_after_retry(database):
    settings, _ = database
    running, release = Event(), Event()

    def slow_runner(*_):
        running.set()
        assert release.wait(timeout=10)
        return draft()

    with (
        TestClient(create_app(settings, slow_runner)) as old_client,
        TestClient(create_app(settings, lambda *_: draft())) as new_client,
        ThreadPoolExecutor(max_workers=1) as executor,
    ):
        intake = upload(old_client)
        slow = executor.submit(old_client.post, f"/intakes/{intake}/process")
        try:
            assert running.wait(timeout=10)
            attempt = new_client.get(f"/intakes/{intake}").json()["attempt"]
            assert new_client.post(f"/intakes/{intake}/process").status_code == 409
            result = new_client.post(f"/intakes/{intake}/interrupt", json={"attempt_id": attempt["id"]})
            assert result.json()["state"] == "failed"
            retried = process(new_client, intake)
            assert retried["state"] == "complete"
        finally:
            release.set()
        assert slow.result(timeout=10).status_code == 409
        assert new_client.get(f"/intakes/{intake}").json() == retried
