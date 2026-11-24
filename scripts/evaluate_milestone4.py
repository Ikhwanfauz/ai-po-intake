"""Compare the baseline and LangGraph on frozen synthetic PDFs without writing app records."""
import argparse
import hashlib
import json
from collections import Counter
from decimal import Decimal, InvalidOperation
from importlib.metadata import version
from pathlib import Path
from statistics import median
from time import perf_counter

import httpx
from sqlalchemy import text
from sqlalchemy.orm import Session

from po_intake import review
from po_intake.config import Settings
from po_intake.db import make_engine
from po_intake.evidence import LINE_FIELDS
from po_intake.extraction import MODEL_OPTIONS, Draft, extract_with_ollama, pdf_pages
from po_intake.extraction_graph import PATCH_TOKENS, REVIEW_TOKENS, graph_issues, run_graph

MANIFEST = Path("output/pdf/milestone4/held-out/manifest.json")
WARMUP = Path("output/pdf/milestone4/demo/PO-201-multipage.pdf")
METRICS = ("expected", "correct", "evidenced", "expected_blank", "correct_blank")


def canonical(field, value):
    if value is None or not str(value).strip():
        return None
    text = str(value).strip()
    if field == "quantity":
        return int(text) if text.isascii() and text.isdigit() else text
    if field == "unit_price":
        try:
            amount = Decimal(text.upper().removeprefix("RM").strip())
            return amount if amount.is_finite() else text
        except InvalidOperation:
            return text
    if field == "currency":
        return "MYR" if text.upper() == "RM" else text.upper()
    return text


def align_lines(expected_lines, proposed_lines):
    available = set(range(len(proposed_lines)))
    aligned = []
    for expected in expected_lines:
        candidates = sorted(index for index in available if proposed_lines[index].sku.value == expected["sku"])
        match = next((index for index in candidates if proposed_lines[index].sku.page == expected["page"]), None)
        if match is None and candidates:
            match = candidates[0]
        if match is not None:
            available.remove(match)
        aligned.append((expected, match))
    return aligned, len(available)


def score(case, draft, assessment):
    evidence = assessment["evidence"]
    fields = {field: Counter({key: 0 for key in METRICS}) for field in (*case["headers"], *LINE_FIELDS)}
    edits = 0

    def count(field, expected, proposed, proof, page, row_present=True):
        nonlocal edits
        expected = canonical(field, expected)
        actual = canonical(field, proposed)
        stat = fields[field]
        if expected is None:
            stat["expected_blank"] += 1
            stat["correct_blank"] += row_present and actual is None
            edits += actual is not None
        else:
            stat["expected"] += 1
            correct = expected == actual
            evidenced = correct and proof.get("status") == "verified" and proof.get("page") == page
            stat["correct"] += correct
            stat["evidenced"] += evidenced
            edits += not evidenced

    for name, expected in case["headers"].items():
        count(name, expected, getattr(draft, name).value, evidence["fields"][name], 1)
    aligned, extra = align_lines(case["lines"], draft.lines)
    for expected, index in aligned:
        for name in LINE_FIELDS:
            proposal = getattr(draft.lines[index], name).value if index is not None else None
            proof = evidence["lines"][index][name] if index is not None else {}
            count(name, expected[name], proposal, proof, expected["page"], row_present=index is not None)
    edits += extra
    validation = assessment["validation"]
    if (review.identity_key(assessment) is None or evidence["issues"] or validation["field_issues"]
            or assessment.get("graph_issues")):
        route = "extraction_review"
    elif validation["price_issues"]:
        route = "business_review"
    else:
        route = "ready"
    return {
        "fields": {name: dict(values) for name, values in fields.items()}, "estimated_edits": edits,
        "missing_rows": sum(index is None for _, index in aligned), "extra_rows": extra,
        "route": route, "route_correct": route == case["expected_route"],
    }


def examine(db, settings, case, pages, *, graph):
    start = perf_counter()
    raw, error = None, None
    if graph:
        result = run_graph(settings, pages, "Forced paired evaluation")
        draft, trace = result.draft, result.trace
        holds = graph_issues(result)
        if draft is None:
            error = trace["reason"]
    else:
        try:
            raw = extract_with_ollama(settings, pages)
            draft = Draft.model_validate_json(raw)
        except (ValueError, OSError, KeyError, TypeError) as exc:
            draft, error = None, f"{type(exc).__name__}: {exc}"
        holds = []
        trace = {"call_count": 1, "rereads": 0, "raw": raw}
    measured = draft or Draft()
    assessment = review.assess(db, measured, pages, None, graph_issues=holds)
    metrics = score(case, measured, assessment)
    if error:
        metrics.update(route="technical_failure", route_correct=False)
    return {**metrics, "elapsed_ms": round((perf_counter() - start) * 1000), "error": error, "trace": trace,
            "proposal": draft.model_dump(mode="json") if draft else None, "issues": assessment["issues"]}


def render_report(report):
    cases = report["cases"]
    lines = [
        "# Milestone 4 paired extraction results", "",
        f"Synthetic held-out corpus **{report['corpus']}**, {len(cases)} PDFs. Both pipelines used "
        f"**{report['model']}** on Ollama **{report['runner']}** with LangGraph **{report['langgraph']}**.",
        "", "One unscored warm-up used the walkthrough PDF. The graph was forced for every pair. "
        "Run order alternated across PDFs. Times include extraction and backend validation.",
        "", "Correct counts expected populated values. Edits estimates source-grounding corrections from "
        "values, evidence, and extra rows.",
        "", "| PDF | Case | Expected routing | Baseline correct / edits / seconds | Graph correct / edits / seconds | Baseline → graph routing | Graph calls / rereads |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]

    def compact(item):
        correct = sum(field["correct"] for field in item["fields"].values())
        return f"{correct} / {item['estimated_edits']} / {item['elapsed_ms'] / 1000:.2f}"

    for case in cases:
        before, after = case["baseline"], case["graph"]
        lines.append(f"| {case['id']} | {case['category']} | {case['expected_route']} | {compact(before)} | "
                     f"{compact(after)} | {before['route']} → {after['route']} | "
                     f"{after['trace']['call_count']} / {after['trace']['rereads']} |")
    lines.extend(["", "| Measure | Baseline | LangGraph |", "| --- | --- | --- |"])
    for label, metric in (("Correct fields", "correct"), ("Correct fields with verified evidence", "evidenced"),
                          ("Correct expected blanks / ambiguities", "correct_blank")):
        counts = []
        for pipeline in ("baseline", "graph"):
            totals = [field for case in cases for field in case[pipeline]["fields"].values()]
            denominator_key = "expected_blank" if metric == "correct_blank" else "expected"
            numerator, denominator = (sum(field[key] for field in totals) for key in (metric, denominator_key))
            counts.append(f"{numerator}/{denominator} ({100 * numerator / denominator:.1f}%)")
        lines.append(f"| {label} | {counts[0]} | {counts[1]} |")
    for label, metric in (("Correct routing", "route_correct"), ("Estimated corrections", "estimated_edits"),
                          ("Missing rows", "missing_rows"), ("Extra rows", "extra_rows")):
        counts = [sum(case[pipeline][metric] for case in cases) for pipeline in ("baseline", "graph")]
        lines.append(f"| {label} | {counts[0]} | {counts[1]} |")
    timings = [median(case[p]["elapsed_ms"] for case in cases) / 1000 for p in ("baseline", "graph")]
    lines.append(f"| Median time (seconds) | {timings[0]:.2f} | {timings[1]:.2f} |")
    failures = [sum(case[p]["error"] is not None for case in cases) for p in ("baseline", "graph")]
    followup_failures = sum(any(event.get("error") for event in case["graph"]["trace"]["handoffs"][1:])
                            for case in cases)
    lines.append(f"| Initial extraction failures | {failures[0]} | {failures[1]} |")
    lines.append(f"| Follow-up call or schema failures with fallback | 0 | {followup_failures} |")
    lines.extend(["", "| Field | Baseline correct | Baseline verified | Graph correct | Graph verified |",
                  "| --- | --- | --- | --- | --- |"])
    for name in cases[0]["baseline"]["fields"]:
        row = []
        for pipeline in ("baseline", "graph"):
            denominator = sum(case[pipeline]["fields"][name]["expected"] for case in cases)
            row.extend(f"{sum(case[pipeline]['fields'][name][metric] for case in cases)}/{denominator}"
                       for metric in ("correct", "evidenced"))
        lines.append(f"| {name} | {' | '.join(row)} |")
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("tmp/milestone4-results"))
    args = parser.parse_args()
    settings = Settings()
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    cases = []
    for item in manifest["cases"]:
        raw = (MANIFEST.parent / item["file"]).read_bytes()
        if hashlib.sha256(raw).hexdigest() != item["sha256"]:
            raise ValueError(f"{item['id']} differs from the frozen manifest.")
        cases.append((item, pdf_pages(raw)))
    with httpx.Client(base_url=settings.ollama_url, timeout=5, trust_env=False) as client:
        runner = client.get("/api/version").raise_for_status().json()["version"]
        tags = client.get("/api/tags").raise_for_status().json()["models"]
        digest = next((item["digest"] for item in tags if item["name"] == settings.model), None)
    extract_with_ollama(settings, pdf_pages(WARMUP.read_bytes()))
    result = {
        "corpus": manifest["version"], "model": settings.model, "model_digest": digest, "runner": runner,
        "langgraph": version("langgraph"), "model_options": MODEL_OPTIONS, "baseline_timeout": settings.timeout,
        "graph_timeout": settings.graph_timeout, "review_tokens": REVIEW_TOKENS, "patch_tokens": PATCH_TOKENS,
        "cases": [],
    }
    engine = make_engine(settings.database_url)
    try:
        with Session(engine) as db:
            db.execute(text("SET TRANSACTION READ ONLY"))
            for index, (case, pages) in enumerate(cases):
                measured = {}
                for pipeline in ("baseline", "graph") if index % 2 == 0 else ("graph", "baseline"):
                    print(f"{case['id']} {pipeline}", flush=True)
                    measured[pipeline] = examine(db, settings, case, pages, graph=pipeline == "graph")
                result["cases"].append({
                    **{name: case[name] for name in ("id", "category", "expected_route", "sha256")},
                    "run_order": list(measured), **measured,
                })
    finally:
        engine.dispose()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "paired-results.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    (args.output_dir / "paired-results.md").write_text(render_report(result), encoding="utf-8")


if __name__ == "__main__":
    main()
