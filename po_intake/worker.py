import argparse
import logging
import time
from dataclasses import dataclass
from datetime import timedelta
from time import perf_counter

import pymupdf
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from po_intake import jobs, notifications, review
from po_intake.config import Settings
from po_intake.db import Attempt, ExtractionJob, GraphRun, Intake, Snapshot, make_engine
from po_intake.extraction import (
    MAX_TEXT_CHARS, Draft, ExtractionOutput, TransientExtractionError, extract_with_ollama, ollama_structured, pdf_pages,
)
from po_intake.extraction_graph import GraphResult, evidence_trigger, graph_issues, run_graph

log = logging.getLogger(__name__)


@dataclass
class ExtractionResult:
    pages: list | None = None
    raw: str | None = None
    draft: Draft | None = None
    error: str | None = None
    output_source: str | None = None
    retryable: bool = False
    elapsed_ms: int = 0
    graph: GraphResult | None = None


def extract(settings, path, extractor):
    started = perf_counter()
    result = ExtractionResult()
    try:
        result.pages = pdf_pages(path.read_bytes())
        if sum(len(page["text"]) for page in result.pages) > MAX_TEXT_CHARS:
            raise ValueError("The PDF exceeds the local extraction text limit.")
        if any(page["text"].strip() for page in result.pages):
            output = extractor(settings, result.pages)
            if isinstance(output, ExtractionOutput):
                result.raw, result.output_source = output.original_raw or output.raw, output.source
                result.draft = Draft.model_validate_json(output.raw)
            else:
                result.raw = output
        if result.draft is None:
            result.draft = Draft.model_validate_json(result.raw) if result.raw is not None else Draft()
    except (TransientExtractionError, OSError) as exc:
        result.error = str(exc) if isinstance(exc, TransientExtractionError) else "Stored PDF could not be read."
        result.retryable = True
    except ValidationError:
        result.error = "Ollama returned a response that does not match the extraction schema."
    except (ValueError, pymupdf.FileDataError) as exc:
        result.error = str(exc)
    except Exception:
        log.exception("Extraction failed unexpectedly")
        result.error = "Extraction failed unexpectedly. Check the worker log and retry."
    result.elapsed_ms = round((perf_counter() - started) * 1000)
    return result


def publish(engine, settings, lease, result, *, automatic_retry=True):
    with Session(engine) as db, db.begin():
        job = db.get(ExtractionJob, lease.intake_id, with_for_update=True)
        now = notifications.clock(db)
        if (not job or job.state != "running" or job.claim_token != lease.token
                or job.attempt_id != lease.attempt_id or job.lease_until <= now):
            return False
        intake = db.get(Intake, lease.intake_id, with_for_update=True)
        attempt = db.get(Attempt, lease.attempt_id)
        attempt.pages, attempt.raw, attempt.error = result.pages, result.raw, result.error
        attempt.finished_at = now
        if result.output_source:
            attempt.settings = {**attempt.settings, "source": result.output_source}

        if result.error is None:
            draft = result.graph.draft if result.graph else result.draft
            holds = graph_issues(result.graph) if result.graph else []
            attempt.draft = draft.model_dump(mode="json")
            attempt.validation = review.store_initial_review(db, intake, attempt, draft, graph_issues=holds)
            if result.graph:
                snapshot = db.get(Snapshot, attempt.id)
                db.add(GraphRun(intake_id=intake.id, source_snapshot_id=snapshot.id, mode="automatic",
                                author="LangGraph", model=settings.model, candidate=attempt.draft,
                                assessment=snapshot.assessment, trace=result.graph.trace))
            attempt.state = intake.state = "complete"
            jobs.end_claim(job, "complete", now)
            notifications.sync_review(db, settings, intake)
        else:
            attempt.state = "failed"
            retry = automatic_retry and result.retryable and job.attempt_count < settings.extraction_max_attempts
            intake.state = "pending" if retry else "failed"
            available = now + timedelta(seconds=settings.extraction_backoff if retry else 0)
            jobs.end_claim(job, intake.state, available)
            if not retry:
                notifications.sync_review(db, settings, intake, result.error)
        return True


def run_one(engine, settings, extractor=extract_with_ollama, *, intake_id=None, automatic_retry=True,
            graph_chat=ollama_structured):
    lease = jobs.claim(engine, settings, intake_id)
    if lease is None:
        return None
    log.info("Claimed intake %s, attempt %s", lease.intake_id, lease.attempt_id)
    result = extract(settings, lease.path, extractor)
    if settings.graph_auto and result.draft is not None and result.output_source != "Demo fixture replay":
        trigger = evidence_trigger(result.draft, result.pages)
        if trigger:
            result.graph = run_graph(
                settings, result.pages, trigger, chat=graph_chat, initial=result.draft,
                initial_raw=result.raw, initial_elapsed_ms=result.elapsed_ms, initial_source=result.output_source,
                fallback=result.draft,
            )
    return publish(engine, settings, lease, result, automatic_retry=automatic_retry)


def main():
    parser = argparse.ArgumentParser(description="Process leased purchase order intake jobs.")
    parser.add_argument("--once", action="store_true", help="Process at most one available job.")
    fixture = parser.add_mutually_exclusive_group()
    fixture.add_argument("--fixture-replay", action="store_true", help="Replay the shipped Milestone 2 fixture drafts.")
    fixture.add_argument("--graph-demo", action="store_true", help="Withhold the PO-201 continuation price for the walkthrough.")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = Settings()
    engine = make_engine(settings.database_url)
    extractor = extract_with_ollama
    if args.fixture_replay:
        from scripts.milestone2_demo import extract_demo
        extractor = extract_demo
    elif args.graph_demo:
        from scripts.milestone4_demo import extract_demo
        extractor = extract_demo
    try:
        if args.once:
            run_one(engine, settings, extractor)
            return
        log.info("Waiting for extraction jobs")
        while True:
            try:
                if run_one(engine, settings, extractor) is None:
                    time.sleep(1)
            except SQLAlchemyError:
                log.exception("Database operation failed. Waiting before checking the queue again.")
                time.sleep(2)
    except KeyboardInterrupt:
        log.info("Extraction worker stopped")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
