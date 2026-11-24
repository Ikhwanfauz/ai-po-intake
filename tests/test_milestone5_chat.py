import json
import socket
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from threading import Thread
from time import monotonic, sleep

import anyio
import httpx
import pytest
import uvicorn
from mcp import Client
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session
from streamlit.testing.v1 import AppTest

from po_intake.api import create_app
from po_intake.chat_context import TOOL_NAME
from po_intake.db import Price, ReviewEvent, SalesOrder, Snapshot, Version
from po_intake.mcp_client import server_parameters
from po_intake.sop_index import SOP_DIR, build, load, search
from scripts.evaluate_milestone5 import CASES, evaluate
from scripts.milestone2_demo import FIXTURE_ROOT, extract_demo
from scripts.milestone5_demo import activate_v2, demo_extractor, seed_fixtures, upload_fixture, write_fixtures
from test_milestone3 import KEY, gmail


def propose_ids(*args, **kwargs):
    return '{"facts": [], "sections": ["SOP-99.rev1.7"]}'


@contextmanager
def live_api(settings, *, mcp_path="", extractor=demo_extractor):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        url = f"http://127.0.0.1:{listener.getsockname()[1]}"
        settings = replace(settings, mcp_api_url=url + mcp_path, staff_key="m5-test-staff", outbound_enabled=False)
        app = create_app(settings, extractor=extractor, chat_model=propose_ids)
        server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="off"))
        thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = monotonic() + 10
            while not server.started and thread.is_alive() and monotonic() < deadline:
                sleep(0.05)
            assert server.started, "Local Uvicorn test server did not start"
            with httpx.Client(base_url=url, headers={"X-Staff-Key": settings.staff_key},
                              trust_env=False, timeout=30) as client:
                yield settings, client
        finally:
            server.should_exit = True
            thread.join(10)
            assert not thread.is_alive(), "Local Uvicorn test server did not stop"


@pytest.fixture
def settings(database, tmp_path):
    config, _ = database
    path = tmp_path / "sop-index.json"
    build(SOP_DIR, path)
    return replace(config, sop_index_path=path)


def chat(client, detail, question):
    return client.post(f"/pos/{detail['po']['id']}/chat", json={"question": question}).raise_for_status().json()


@pytest.mark.parametrize(("question", "expected"), [
    ("Can I sign off after checking the buyer account and units ordered?", "SOP-02.rev1.3"),
    ("What should we do when the same PDF is sent again as a revised order?", "SOP-01.rev1.2"),
    ("What steps apply when the printed unit costs disagree with the agreed price?", "SOP-02.rev1.1"),
    ("Does the warehouse open on Sundays?", None),
])
def test_heading_index_development_questions(tmp_path, question, expected):
    path = tmp_path / "index.json"
    build(SOP_DIR, path)
    results = search(load(path), question)
    assert (results[0]["key"] if results else None) == expected


def test_index_rejects_modified_passage(tmp_path):
    path = tmp_path / "index.json"
    index = build(SOP_DIR, path)
    index["sections"][0]["excerpt"] = "Fabricated authority"
    path.write_text(json.dumps(index), encoding="utf-8")
    with pytest.raises(ValueError, match="differ from their source"):
        load(path)


def test_real_mcp_acceptance_and_holdout(settings, database, tmp_path):
    _, engine = database

    def state():
        with Session(engine) as db:
            return [db.scalar(select(func.count()).select_from(model))
                    for model in (ReviewEvent, SalesOrder, Snapshot, Version)]

    with live_api(settings) as (live, client):
        receipts = seed_fixtures(client, tmp_path / "fixtures")
        first = receipts["PO-106-V2.pdf"]["po"]["id"]
        second = receipts["PO-107-V1.pdf"]["po"]["id"]
        before = state()

        assert client.post(f"/pos/{first}/chat", json={"question": "Price?", "po_id": second}).status_code == 422
        assert httpx.get(f"{live.mcp_api_url}/pos/{first}/review-context", trust_env=False).status_code == 401

        async def reject_other_po():
            async with Client(server_parameters(live, first)) as mcp:
                tools = (await mcp.list_tools()).tools
                assert len(tools) == 1
                assert tools[0].name == TOOL_NAME and tools[0].annotations.read_only_hint
                result = await mcp.call_tool(TOOL_NAME, {"po_id": second})
                assert result.is_error and result.structured_content is None

        anyio.run(reject_other_po)
        results = evaluate(client)
        assert len(results["runs"]) == 7
        for run in results["runs"]:
            assert run["passed"], (run["case"]["id"], run["checks"], run["answer"])
        assert state() == before
        assert before[1] == 0


def test_unavailable_mcp_still_shows_supported_sop(settings, tmp_path):
    question = json.loads(CASES.read_text(encoding="utf-8"))[0]["question"]
    with live_api(settings, mcp_path="/unavailable") as (_, client):
        detail = seed_fixtures(client, tmp_path / "fixtures")["PO-106-V2.pdf"]
        answer = chat(client, detail, question)
        trace = answer["trace"]["mcp"]
        assert trace["tool"] == TOOL_NAME and trace["result"] is None and trace["error"]
        assert answer["live_facts"]["reference"] is None
        assert "could not be verified" in answer["live_facts"]["facts"][0]
        assert [item["key"] for item in answer["sop_guidance"]["citations"]] == ["SOP-02.rev1.3"]


def test_same_dashboard_receipt_refreshes_after_revision(settings, monkeypatch, tmp_path):
    directory = tmp_path / "fixtures"
    write_fixtures(directory)
    question = "Which unit price and revision are active on this PO?"
    with live_api(settings) as (live, client):
        v1 = upload_fixture(client, directory / "PO-106-V1.pdf")
        v2 = upload_fixture(client, directory / "PO-106-V2.pdf")
        pending = chat(client, v1, question)
        assert pending["trace"]["mcp"]["result"]["latest_candidate"]["disposition"] == "pending"
        assert pending["live_facts"]["issues"][-1] == "Pending document revisions: V2."

        monkeypatch.setenv("PO_API_URL", live.mcp_api_url)
        monkeypatch.setenv("PO_STAFF_KEY", live.staff_key)
        page = AppTest.from_file(Path(__file__).resolve().parents[1] / "dashboard.py", default_timeout=20)
        page.query_params["intake"] = str(v1["id"])
        page.run()
        page.chat_input[0].set_value(question).run()
        assert not page.exception
        assert any("PO-106 V1" in item.value for item in page.text)
        assert any("checked unit price MYR 90.00" in item.value for item in page.text)
        assert any(item.label == "MCP tool trace" for item in page.expander)

        active = activate_v2(client, v2)
        page.run()
        assert list(page.chat_message) == []
        page.chat_input[0].set_value(question).run()
        assert not page.exception
        assert page.button(key=f"open-intake-{v1['id']}").label == f"Selected #{v1['id']}"
        assert any(f"PO-106 V2 · snapshot {active['snapshot']['id']}" == item.value for item in page.text)
        assert any("checked unit price MYR 100.00" in item.value for item in page.text)
        assert all("checked unit price MYR 90.00" not in item.value for item in page.text)


def test_receipt_list_opens_po104_revisions_and_preserves_review(settings, monkeypatch):
    settings = replace(settings, integration_key=KEY, gmail_senders={"buyer@example.test": "CUST-101"})
    with live_api(settings, extractor=extract_demo) as (live, client):
        v1 = gmail(client, "PO-104-V1.pdf", "list-v1")
        client.post(f"/intakes/{v1['id']}/process").raise_for_status()
        v2 = upload_fixture(client, FIXTURE_ROOT / "PO-104-V2.pdf")
        v1_button, v2_button = (f"open-intake-{receipt['id']}" for receipt in (v1, v2))

        monkeypatch.setenv("PO_API_URL", live.mcp_api_url)
        monkeypatch.setenv("PO_STAFF_KEY", live.staff_key)
        dashboard = Path(__file__).resolve().parents[1] / "dashboard.py"
        page = AppTest.from_file(dashboard, default_timeout=20).run()
        assert not page.exception
        assert all(widget.label != "Intake receipt" for widget in page.selectbox)
        assert [item.value for item in page.text[:2]] == ["PO-104-V2.pdf", "PO-104-V1.pdf"]
        received = datetime.fromisoformat(v1["received_at"]).astimezone(timezone.utc)
        assert f"Gmail · {received:%Y-%m-%d %H:%M:%S UTC} · Complete" in [item.value for item in page.caption]
        assert any(item.value.startswith("Upload · ") and item.value.endswith(" · Complete") for item in page.caption)

        page.button(key=v1_button).click().run()
        assert page.button(key=v1_button).label == f"Selected #{v1['id']}"
        assert any("V1 · Active" == item.value for item in page.subheader)
        assert any("RM90.00 differs from agreed RM100.00" in item.value for item in page.warning)

        page.button(key=v2_button).click().run()
        assert any("V2 · Pending revision" == item.value for item in page.subheader)
        assert any("Compare with active V1" == item.value for item in page.subheader)
        page.text_input(key=f"revision-reason-{v2['snapshot']['id']}").set_value("Verified V2 against the PDF.")
        next(button for button in page.button if button.label == "Activate revision").click().run()
        assert any("V2 · Active" == item.value for item in page.subheader)

        resend = gmail(client, "PO-104-V1.pdf", "list-v1-resend")
        page.button(key="refresh-intakes").click().run()
        assert page.button(key=v2_button).label == f"Selected #{v2['id']}"
        assert [button.key for button in page.button if (button.key or "").startswith("open-intake-")] == [
            f"open-intake-{resend['id']}", v2_button, v1_button,
        ]
        assert any(item.value.startswith("Gmail · ") and item.value.endswith(" · Complete · Exact resend")
                   for item in page.caption)

        page.checkbox(key=f"checked-{v2['snapshot']['id']}").check().run()
        next(button for button in page.button if button.label == "Approve snapshot").click().run()
        assert any("Confirmed order" in item.value for item in page.success)
        assert len(client.get("/orders").raise_for_status().json()) == 1

        page.chat_input[0].set_value("Which unit price and revision are active on this PO?").run()
        assert not page.exception
        assert any(f"PO-104 V2 · snapshot {v2['snapshot']['id']}" == item.value for item in page.text)
        assert any("checked unit price MYR 100.00" in item.value for item in page.text)
        assert any("Already approved as SO-" in item.value for item in page.text)
        assert any(item.label == "MCP tool trace" for item in page.expander)

        linked_page = AppTest.from_file(dashboard, default_timeout=20)
        linked_page.query_params["intake"] = str(v1["id"])
        linked_page.run()
        assert linked_page.button(key=v1_button).label == f"Selected #{v1['id']}"
        assert any("V1 · Superseded" == item.value for item in linked_page.subheader)


def test_approval_and_current_price_conflict(settings, database, tmp_path):
    _, engine = database
    question = json.loads(CASES.read_text(encoding="utf-8"))[0]["question"]
    with live_api(settings) as (_, client):
        detail = seed_fixtures(client, tmp_path / "fixtures")["PO-106-V2.pdf"]
        with engine.begin() as db:
            db.execute(update(Price).where(Price.customer == "CUST-101", Price.sku == "BRG-6204").values(amount=101))
        conflict = chat(client, detail, question)["live_facts"]
        assert "premise is false" in conflict["premise"]
        assert any("conflicts with the current backend checks" in issue for issue in conflict["issues"])
        assert any("RM100.00 differs from agreed RM101.00" in issue for issue in conflict["issues"])
        with engine.begin() as db:
            db.execute(update(Price).where(Price.customer == "CUST-101", Price.sku == "BRG-6204").values(amount=100))
        approval = client.post(f"/intakes/{detail['id']}/approve", json={"snapshot_id": detail["snapshot"]["id"]})
        number = approval.raise_for_status().json()["number"]
        approved = chat(client, detail, question)
        assert f"Already approved as {number}" in approved["live_facts"]["status"]
        assert approved["trace"]["mcp"]["result"]["status"] == "ordered"
