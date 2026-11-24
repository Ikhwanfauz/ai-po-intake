from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from po_intake import notifications
from po_intake.db import Attempt, Document, ExtractionJob, Intake
from po_intake.extraction import MODEL_OPTIONS


def enqueue(db, intake_id):
    db.execute(insert(ExtractionJob).values(intake_id=intake_id).on_conflict_do_nothing())


def retry_failed(db, intake_id):
    enqueue(db, intake_id)
    job = db.get(ExtractionJob, intake_id, with_for_update=True)
    intake = db.get(Intake, intake_id, with_for_update=True, populate_existing=True)
    if intake.state == "failed" and job.state in {"failed", "pending"}:
        job.state, job.attempt_count = "pending", 0
        job.available_at = notifications.clock(db)
        intake.state = "pending"
    return intake.state


def end_claim(job, state, available_at):
    job.state, job.available_at = state, available_at
    job.claim_token = job.lease_until = None


@dataclass(frozen=True)
class Claim:
    intake_id: int
    attempt_id: int
    token: str
    path: Path


def claim(engine, settings, intake_id=None):
    lease_seconds = settings.extraction_lease or max(30, int(settings.timeout) + 30)
    with Session(engine) as db, db.begin():
        while True:
            now = notifications.clock(db)
            query = select(ExtractionJob).where(or_(
                (ExtractionJob.state == "pending") & (ExtractionJob.available_at <= now),
                (ExtractionJob.state == "running") & (ExtractionJob.lease_until <= now),
            ))
            if intake_id is not None:
                query = query.where(ExtractionJob.intake_id == intake_id)
            job = db.scalar(query.order_by(ExtractionJob.available_at, ExtractionJob.intake_id)
                            .limit(1).with_for_update(skip_locked=True))
            if job is None:
                return None
            intake = db.get(Intake, job.intake_id, with_for_update=True)
            if job.state == "running":
                previous = db.get(Attempt, job.attempt_id)
                previous.state, previous.finished_at = "failed", now
                previous.error = "Worker lease expired before extraction finished."
            if job.attempt_count >= settings.extraction_max_attempts:
                intake.state = "failed"
                end_claim(job, "failed", now)
                notifications.sync_review(db, settings, intake, "Extraction attempts exhausted after worker interruption.")
                continue
            attempt = Attempt(
                intake_id=intake.id, state="processing",
                settings={"model": settings.model, "think": False, "timeout": settings.timeout, **MODEL_OPTIONS},
            )
            intake.state = "processing"
            db.add(attempt)
            db.flush()
            job.claim_token = uuid4().hex
            job.lease_until = now + timedelta(seconds=lease_seconds)
            job.attempt_count += 1
            job.attempt_id, job.state = attempt.id, "running"
            document = db.get(Document, intake.digest)
            return Claim(intake.id, attempt.id, job.claim_token, settings.storage / document.path)


def interrupt(db, intake_id, attempt_id, reviewer):
    job = db.get(ExtractionJob, intake_id, with_for_update=True)
    if not job or job.state != "running" or job.attempt_id != attempt_id:
        return False
    intake = db.get(Intake, intake_id, with_for_update=True)
    attempt = db.get(Attempt, attempt_id)
    now = notifications.clock(db)
    attempt.state = intake.state = "failed"
    attempt.error = f"Attempt marked interrupted by {reviewer}."
    attempt.finished_at = now
    end_claim(job, "failed", now)
    return True
