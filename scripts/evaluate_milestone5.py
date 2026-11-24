"""Run the frozen synthetic chat cases against the local API and save full traces."""
import argparse
import json
from importlib.metadata import version
from pathlib import Path
from time import perf_counter

import httpx

from po_intake.chat_context import TOOL_NAME
from po_intake.config import Settings
from po_intake.sop_index import SOP_DIR, corpus, extract_sections


CASES = Path(__file__).resolve().parents[1] / "fixtures" / "milestone5-chat.json"


def assertions(case, snapshot, answer, sources):
    live = answer["live_facts"]
    sop = answer["sop_guidance"]
    mcp = answer["trace"]["mcp"]
    tool_result = mcp["result"] or {}
    citations = sop["citations"]
    live_text = " ".join([live["reference"] or "", live["status"] or "", live["premise"] or "", *live["facts"]])
    return {
        "tool_call": (mcp["error"] is None and mcp["discovered_tools"] == [TOOL_NAME]
                      and mcp["tool"] == TOOL_NAME and mcp["arguments"] == {"po_id": snapshot["po_id"]}),
        "frozen_snapshot": (tool_result.get("po_id") == snapshot["po_id"]
                            and tool_result.get("active_document") == snapshot["active_document"]
                            and tool_result.get("status") == snapshot["status"]
                            and tool_result.get("latest_candidate") == snapshot["latest_candidate"]
                            and tool_result.get("issues") == snapshot["issues"]
                            and tool_result.get("checked_lines") == snapshot["checked_lines"]),
        "supported_claims": all(expected in live_text for expected in case.get("live_contains", [])),
        "reported_issues": (len(live["issues"]) == len(case["issues_contain"])
                            and all(any(expected in issue for issue in live["issues"])
                                    for expected in case["issues_contain"])),
        "citations": ([citation["key"] for citation in citations] == case["citations"]
                      and all(all(citation[key] == value for key, value in sources[citation["key"]].items())
                              for citation in citations)),
        "sop_message": case.get("sop_contains", "") in sop["message"],
        "abstention": (not case.get("abstention")
                       or (live["reference"] is None and live["status"] is None and not citations)),
    }


def evaluate(client):
    cases = json.loads(CASES.read_text(encoding="utf-8"))
    by_name = {row["filename"]: row for row in reversed(client.get("/intakes").raise_for_status().json())}
    digest, documents = corpus(SOP_DIR)
    sources = {section["key"]: section for section in extract_sections(documents)}
    snapshots = {}
    for case in cases:
        filename = case["receipt"]
        if filename not in snapshots:
            receipt = by_name[filename]
            detail = client.get(f"/intakes/{receipt['id']}").raise_for_status().json()
            snapshots[filename] = client.get(f"/pos/{detail['po']['id']}/review-context").raise_for_status().json()
        snapshot = snapshots[filename]
        expected_status = "ready_for_approval" if case["version"] == 2 else "needs_review"
        if (snapshot["status"] != expected_status or snapshot["active_document"]["version"] != case["version"]
                or snapshot["checked_lines"][0]["checked_unit_price"] != case["price"]):
            raise ValueError(f"{filename} no longer matches the frozen fixture. Use a fresh demo database.")

    runs = []
    for case in cases:
        snapshot = snapshots[case["receipt"]]
        started = perf_counter()
        answer = client.post(f"/pos/{snapshot['po_id']}/chat", json={
            "question": case["question"],
        }).raise_for_status().json()
        checks = assertions(case, snapshot, answer, sources)
        runs.append({"case": case, "elapsed_seconds": round(perf_counter() - started, 3), "checks": checks,
                     "passed": all(checks.values()), "answer": answer})
    return {"mcp_sdk": version("mcp"), "corpus_version": digest, "snapshots": snapshots, "runs": runs}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("data/milestone5/chat-results.json"))
    args = parser.parse_args()
    settings = Settings()
    headers = {"X-Staff-Key": settings.staff_key} if settings.staff_key else {}
    with httpx.Client(base_url=settings.mcp_api_url, headers=headers, trust_env=False,
                      timeout=settings.mcp_timeout + settings.chat_timeout + 5) as client:
        report = evaluate(client)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    for run in report["runs"]:
        failed = ", ".join(key for key, passed in run["checks"].items() if not passed)
        print(f"{run['case']['id']}: {failed or 'PASS'}")
    print(f"Wrote {args.output}")
    if not all(run["passed"] for run in report["runs"]):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
