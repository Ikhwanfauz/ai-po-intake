import logging
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import PureWindowsPath
from typing import Literal

import pymupdf
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from po_intake import review
from po_intake.config import Settings
from po_intake.db import (
    Attempt, Customer, Document, Intake, OrderLine, PurchaseOrder, ReviewEvent, SalesOrder,
    Snapshot, Version, make_engine,
)
from po_intake.evidence import HEADERS, quote_location, supported_correction
from po_intake.extraction import (
    MAX_PDF_BYTES, MAX_TEXT_CHARS, MODEL_OPTIONS, Draft, DraftLine, ExtractionOutput, Proposal,
    extract_with_ollama, open_pdf, pdf_pages, store_pdf,
)

log = logging.getLogger(__name__)


class Approve(BaseModel):
    snapshot_id: int = Field(gt=0)


class ReviewRequest(Approve):
    active_snapshot_id: int | None
    reason: str = Field(min_length=1, max_length=2000, pattern=r"\S")


class Correction(ReviewRequest):
    field: Literal["po_number", "customer_reference", "currency", "sku", "quantity", "unit_price", "unit", "remove_line"]
    line: int | None = Field(default=None, gt=0)
    value: str | None = Field(default=None, max_length=256)
    page: int = Field(gt=0)
    excerpt: str = Field(min_length=1, max_length=4000)
    confirm_customer: bool = False


class Interrupt(BaseModel):
    attempt_id: int = Field(gt=0)


def get_intake(db, intake_id, lock=False):
    intake = db.get(Intake, intake_id, with_for_update=lock, populate_existing=lock)
    if intake is None:
        raise HTTPException(404, "Intake not found.")
    return intake


def work_intake(db, intake_id, lock=False):
    receipt = get_intake(db, intake_id)
    return get_intake(db, receipt.canonical_id or receipt.id, lock)


def latest_attempt(db, intake_id):
    return db.scalar(
        select(Attempt).where(Attempt.intake_id == intake_id).order_by(Attempt.id.desc()).limit(1)
    )


def intake_summary(intake, work):
    return {
        "id": intake.id, "filename": intake.filename, "state": work.state,
        "received_at": intake.received_at, "customer_context": intake.customer_context,
        "canonical_intake_id": intake.canonical_id, "context_issue": intake.context_issue, "source": intake.source,
    }


def order_summary(order):
    return {
        "id": order.id, "number": f"SO-{order.id:06d}", "po_number": order.po_number,
        "customer": order.customer, "snapshot_id": order.snapshot_id, "intake_id": order.intake_id,
        "currency": order.currency, "reviewer": order.reviewer, "approved_at": order.approved_at,
        "po_id": order.po_id,
    }


def version_summary(version):
    return {"id": version.id, "intake_id": version.intake_id, "number": version.number,
            "disposition": version.disposition}


def conflict(intake, message, issues=None):
    detail = {"message": message, "refresh": f"/intakes/{intake.id}"}
    if issues:
        detail["issues"] = issues
    return JSONResponse(status_code=409, content={"detail": detail})


def lock_review(db, intake_id):
    intake = get_intake(db, intake_id, lock=True)
    if intake.canonical_id:
        return intake, None, None, None
    version = review.version_for(db, intake_id)
    if not version:
        return intake, None, None, None
    po = review.lock_po(db, version.po_id)
    if po:
        db.refresh(version)
    return intake, version, review.current_snapshot(db, version), po


def assess_snapshot(db, intake, snapshot, po, lock_prices=False):
    initial = snapshot.assessment
    context = initial["context"] if initial else intake.customer_context
    draft = Draft.model_validate(snapshot.draft)
    attempt = db.get(Attempt, snapshot.attempt_id)
    assessment = review.assess(db, draft, attempt.pages, context, intake.context_issue, lock_prices)
    if po and review.identity_key(assessment) != (po.customer, po.po_key):
        issue = "The verified identity no longer matches the linked PO."
        assessment["issues"].append(issue)
        assessment["validation"]["field_issues"].append(issue)
    return assessment


def create_app(settings=None, extractor=extract_with_ollama):
    settings = settings or Settings()
    engine = make_engine(settings.database_url)
    app = FastAPI(title="Meridian PO Intake")

    def original(db, intake_id):
        intake = get_intake(db, intake_id)
        document = db.get(Document, intake.digest)
        try:
            return (settings.storage / document.path).read_bytes()
        except OSError as exc:
            raise HTTPException(503, "Stored original is currently unavailable.") from exc

    @app.get("/customers")
    def customers():
        with Session(engine) as db:
            return [
                {"reference": customer.reference, "name": customer.name}
                for customer in db.scalars(select(Customer).order_by(Customer.reference))
            ]

    @app.post("/intakes", status_code=201)
    def upload(file: UploadFile = File(), customer_context: str | None = Form(None)):
        data = file.file.read(MAX_PDF_BYTES + 1)
        if not data or len(data) > MAX_PDF_BYTES:
            raise HTTPException(422, "Upload a nonempty PDF of at most 10 MiB.")
        try:
            with open_pdf(data):
                pass
        except (ValueError, pymupdf.FileDataError) as exc:
            raise HTTPException(422, "Upload a readable, unencrypted PDF with 1 to 20 pages.") from exc
        with Session(engine) as db, db.begin():
            context = customer_context or None
            if context and db.get(Customer, context) is None:
                raise HTTPException(422, "Selected customer does not exist.")
            try:
                digest, path = store_pdf(settings.storage, data)
            except OSError as exc:
                log.exception("Could not store original PDF")
                raise HTTPException(503, "Could not store the original PDF.") from exc
            db.execute(insert(Document).values(
                digest=digest, path=path, size=len(data)
            ).on_conflict_do_nothing())
            db.get(Document, digest, with_for_update=True)
            canonical, issue = review.exact_delivery(db, digest, context)
            intake = Intake(
                digest=digest, filename=PureWindowsPath(file.filename or "order.pdf").name,
                customer_context=context, canonical_id=canonical.id if canonical else None, context_issue=issue,
            )
            db.add(intake)
            db.flush()
            return intake_summary(intake, canonical or intake)

    @app.get("/intakes")
    def intakes():
        with Session(engine) as db:
            rows = db.scalars(select(Intake).order_by(Intake.id.desc())).all()
            by_id = {intake.id: intake for intake in rows}
            return [intake_summary(intake, by_id[intake.canonical_id] if intake.canonical_id else intake)
                    for intake in rows]

    @app.get("/intakes/{intake_id}")
    def intake_detail(intake_id: int):
        # Read a coherent view when a candidate is being activated or an edit is being saved.
        with engine.connect().execution_options(isolation_level="REPEATABLE READ") as connection, Session(connection) as db:
            receipt = get_intake(db, intake_id)
            work = work_intake(db, intake_id)
            result = intake_summary(receipt, work)
            result.update(attempt=None, order=None, snapshot=None, po=None, version=None, identity_options=None,
                          history=[], activity=[], comparison=None, receipt_edit_history=[])
            attempt = latest_attempt(db, work.id)
            if not attempt:
                return result
            result["attempt"] = {
                "id": attempt.id, "state": attempt.state, "started_at": attempt.started_at,
                "finished_at": attempt.finished_at, "settings": attempt.settings, "error": attempt.error,
                "draft": attempt.draft, "raw": attempt.raw, "validation": attempt.validation, "pages": attempt.pages,
            }
            version = review.version_for(db, work.id)
            if not version:
                return result
            snapshot = review.current_snapshot(db, version)
            result["snapshot"] = review.snapshot_view(db, snapshot)
            result["version"] = version_summary(version)
            po = db.get(PurchaseOrder, version.po_id) if version.po_id else None
            if po:
                order = db.scalar(select(SalesOrder).where(SalesOrder.po_id == po.id))
                result["order"] = order_summary(order) if order else None
                active_version = db.get(Version, po.active_version_id)
                result["po"] = {"id": po.id, "status": po.status, "active_intake_id": active_version.intake_id,
                                "active_version": active_version.number, "active_snapshot_id": po.active_snapshot_id,
                                "pending_versions": review.blockers(db, po)}
                if po.active_version_id != version.id:
                    active = review.snapshot_view(db, db.get(Snapshot, po.active_snapshot_id))
                    candidate = result["snapshot"]
                    result["comparison"] = {
                        "active_intake_id": active_version.intake_id, "active_snapshot_id": active["id"],
                        "active_currency": active["draft"]["currency"]["value"],
                        "candidate_currency": candidate["draft"]["currency"]["value"],
                        "lines": review.diff_lines(active, candidate),
                    }
                versions = db.scalars(select(Version).where(Version.po_id == po.id).order_by(Version.number)).all()
            else:
                assessment = result["snapshot"]["assessment"]
                if assessment:
                    result["identity_options"] = review.identity_options(
                        db, Draft.model_validate(snapshot.draft), attempt.pages, assessment
                    )
                versions = [version]
            for entry in versions:
                deliveries = db.scalars(select(Intake).where(
                    (Intake.id == entry.intake_id) | (Intake.canonical_id == entry.intake_id)
                ).order_by(Intake.id)).all()
                edits = db.scalars(select(Snapshot).where(Snapshot.version_id == entry.id).order_by(Snapshot.edit)).all()
                result["history"].append({
                    **version_summary(entry), "filename": deliveries[0].filename,
                    "receipts": [intake_summary(item, db.get(Intake, entry.intake_id)) for item in deliveries],
                    "edits": [review.snapshot_view(db, item) for item in edits],
                })
            if receipt.id != work.id:
                receipt_version = review.version_for(db, receipt.id)
                if receipt_version:
                    versions.append(receipt_version)
                    result["receipt_edit_history"] = [
                        review.snapshot_view(db, item) for item in db.scalars(select(Snapshot).where(
                            Snapshot.version_id == receipt_version.id,
                        ).order_by(Snapshot.edit))
                    ]
            events = db.scalars(select(ReviewEvent).where(
                ReviewEvent.version_id.in_([entry.id for entry in versions])
            ).order_by(ReviewEvent.id)).all()
            result["activity"] = [
                {"action": event.action, "author": event.author, "snapshot_id": event.snapshot_id,
                 "created_at": event.created_at, "detail": event.detail} for event in events
            ]
            return result

    @app.get("/intakes/{intake_id}/pdf")
    def download(intake_id: int):
        with Session(engine) as db:
            return Response(original(db, intake_id), media_type="application/pdf",
                            headers={"Content-Disposition": "attachment; filename=original.pdf"})

    @app.get("/intakes/{intake_id}/pages/{page}")
    def preview(intake_id: int, page: int):
        with Session(engine) as db:
            data = original(db, intake_id)
        with open_pdf(data) as pdf:
            if not 1 <= page <= len(pdf):
                raise HTTPException(404, "PDF page not found.")
            source = pdf[page - 1]
            scale = 1000 / max(source.rect.width, source.rect.height, 1)
            png = source.get_pixmap(matrix=pymupdf.Matrix(scale, scale)).tobytes("png")
            return Response(png, media_type="image/png", headers={"X-Page-Count": str(len(pdf))})

    @app.post("/intakes/{intake_id}/process")
    def process(intake_id: int):
        with Session(engine) as db, db.begin():
            intake = work_intake(db, intake_id, lock=True)
            if intake.state not in {"pending", "failed"}:
                if intake.id != intake_id:
                    return intake_detail(intake_id)
                return conflict(intake, f"Intake is already {intake.state}. Refresh its review.")
            path = settings.storage / db.get(Document, intake.digest).path
            attempt = Attempt(
                intake_id=intake.id, state="processing",
                settings={"model": settings.model, "think": False, "timeout": settings.timeout, **MODEL_OPTIONS},
            )
            intake.state = "processing"
            db.add(attempt)
            db.flush()
            attempt_id, work_id = attempt.id, intake.id

        pages = raw = draft = error = output_source = None
        try:
            pages = pdf_pages(path.read_bytes())
            if sum(len(page["text"]) for page in pages) > MAX_TEXT_CHARS:
                raise ValueError("The PDF exceeds the local extraction text limit.")
            result = extractor(settings, pages) if any(page["text"].strip() for page in pages) else None
            if isinstance(result, ExtractionOutput):
                raw, output_source = result.raw, result.source
            else:
                raw = result
            draft = Draft.model_validate_json(raw) if raw is not None else Draft()
        except ValidationError:
            error = "Ollama returned a response that does not match the extraction schema."
        except (ValueError, OSError, pymupdf.FileDataError) as exc:
            error = str(exc)
        except Exception:
            log.exception("Extraction failed for intake %s", work_id)
            error = "Extraction failed unexpectedly. Check the API log and retry."

        with Session(engine) as db, db.begin():
            intake = get_intake(db, work_id, lock=True)
            attempt = latest_attempt(db, work_id)
            if attempt.id != attempt_id or attempt.state != "processing":
                return conflict(intake, "This extraction attempt is no longer current.")
            attempt.pages, attempt.raw, attempt.error = pages, raw, error
            if output_source:
                attempt.settings = {**attempt.settings, "source": output_source}
            attempt.finished_at = datetime.now(timezone.utc)
            if error is not None:
                attempt.state = intake.state = "failed"
            else:
                attempt.draft = draft.model_dump(mode="json")
                attempt.validation = review.store_initial_review(db, intake, attempt, draft)
                attempt.state = intake.state = "complete"
        return intake_detail(intake_id)

    @app.post("/intakes/{intake_id}/interrupt")
    def interrupt(intake_id: int, request: Interrupt):
        with Session(engine) as db, db.begin():
            intake = work_intake(db, intake_id, lock=True)
            attempt = latest_attempt(db, intake.id)
            if intake.state != "processing" or attempt.id != request.attempt_id:
                return conflict(intake, "The processing attempt changed. Refresh its review.")
            attempt.state = intake.state = "failed"
            attempt.error = f"Attempt marked interrupted by {settings.reviewer}."
            attempt.finished_at = datetime.now(timezone.utc)
        return intake_detail(intake_id)

    def check_expected(db, intake, version, snapshot, po, request, action):
        expected = po.active_snapshot_id if po else None
        if not snapshot or request.snapshot_id != snapshot.id or request.active_snapshot_id != expected:
            if version:
                review.record(db, version, snapshot, f"{action}_blocked", settings.reviewer,
                              {"reason": "Stale snapshot", "submitted": request.snapshot_id,
                               "submitted_active": request.active_snapshot_id, "current_active": expected})
            return conflict(intake, "The reviewed snapshot changed. Refresh and review the current version.")

    @app.post("/intakes/{intake_id}/correct")
    def correct(intake_id: int, request: Correction):
        with Session(engine) as db, db.begin():
            intake, version, snapshot, po = lock_review(db, intake_id)
            stale = check_expected(db, intake, version, snapshot, po, request, "edit")
            if stale is not None:
                return stale
            if version.disposition not in {"unlinked", "active", "pending", "post_order"} or (
                po and po.status == "ordered" and version.disposition != "post_order"
            ):
                return conflict(intake, "This version is closed for edits.")
            attempt = db.get(Attempt, snapshot.attempt_id)
            draft = Draft.model_validate(snapshot.draft)
            draft_before = draft.model_dump(mode="json")
            context = snapshot.assessment["context"] if snapshot.assessment else intake.customer_context
            field, line_number = request.field, request.line
            if (field in HEADERS) != (line_number is None):
                raise HTTPException(422, "Select a line only for line fields.")
            if line_number is None:
                previous = draft_before[field]["value"]
            elif line_number <= len(draft.lines):
                line_before = draft_before["lines"][line_number - 1]
                previous = line_before if field == "remove_line" else line_before[field]
                if field not in {"unit", "remove_line"}:
                    previous = previous["value"]
            else:
                previous = None
            try:
                proposed = Proposal(value=request.value, page=request.page, excerpt=request.excerpt)
            except ValidationError as exc:
                raise HTTPException(422, "Invalid field value.") from exc
            if field == "remove_line":
                if line_number > len(draft.lines):
                    raise HTTPException(422, "Select an existing line to remove.")
                page = next((item for item in attempt.pages if item["page"] == request.page), None)
                location, issue = quote_location(page["text"], request.excerpt) if page else (None, "Invalid page.")
                if not location:
                    raise HTTPException(422, issue)
                draft.lines.pop(line_number - 1)
            else:
                if line_number is not None and line_number > len(draft.lines):
                    if line_number != len(draft.lines) + 1 or field != "sku":
                        raise HTTPException(422, "Start a new line by recording its SKU.")
                    draft.lines.append(DraftLine())
                finding = supported_correction(draft, attempt.pages, field, proposed, line_number)
                if finding["status"] != "verified":
                    raise HTTPException(422, finding["issue"])
                target = draft if line_number is None else draft.lines[line_number - 1]
                setattr(target, field, proposed.value if field == "unit" else proposed)

            if request.confirm_customer:
                if field != "customer_reference" or po:
                    raise HTTPException(422, "Confirm context using the evidenced customer on an unlinked intake.")
                confirmed = review.assess(db, draft, attempt.pages, None)["validation"]["customer"]
                if confirmed is None:
                    raise HTTPException(422, "The evidenced customer must be in the customer catalog.")
                db.get(Document, intake.digest, with_for_update=True)
                context = confirmed
                intake.context_issue = None
                review.isolate_conflicting_resends(db, intake, context)
            assessment = review.assess(db, draft, attempt.pages, context, intake.context_issue)
            if po and review.identity_key(assessment) != (po.customer, po.po_key):
                raise HTTPException(422, "This edit would change the verified identity of a linked PO.")
            if draft_before == draft.model_dump(mode="json") and not request.confirm_customer:
                raise HTTPException(422, "The correction does not change a value or its evidence.")
            edited = Snapshot(
                id=review.next_edit_id(db), version_id=version.id, attempt_id=attempt.id,
                parent_id=snapshot.id, edit=snapshot.edit + 1,
                author=settings.reviewer, reason=request.reason.strip(), draft=draft.model_dump(mode="json"),
                assessment=assessment,
            )
            db.add(edited)
            db.flush()
            review.record(db, version, edited, "corrected", settings.reviewer, {
                "field": field, "line": line_number, "reason": edited.reason,
                "previous_snapshot_id": snapshot.id, "page": request.page, "excerpt": request.excerpt,
                "previous_value": previous, "value": request.value,
                "original_customer_context": intake.customer_context,
                "confirmed_customer_context": context if request.confirm_customer else None,
            })
            if po is None:
                review.link_version(db, intake, version, edited, assessment, settings.reviewer)
            else:
                if version.id == po.active_version_id:
                    po.active_snapshot_id = edited.id
                    review.reconcile_pending(db, po, settings.reviewer)
                elif review.equivalent(db, po, assessment):
                    version.disposition = "equivalent"
                    review.record(db, version, edited, "equivalent", settings.reviewer, {"version": version.number})
                review.update_status(db, po)
        return intake_detail(intake_id)

    @app.post("/intakes/{intake_id}/validate")
    def revalidate(intake_id: int, request: Approve):
        with Session(engine) as db, db.begin():
            intake, version, snapshot, po = lock_review(db, intake_id)
            if not snapshot or snapshot.id != request.snapshot_id or (
                po and po.status == "ordered" and version.disposition != "post_order"
            ):
                return conflict(intake, "Snapshot is unavailable or changed. Refresh its review.")
            assessment = assess_snapshot(db, intake, snapshot, po)
            review.record(db, version, snapshot, "validation", settings.reviewer, assessment)
            if po:
                review.update_status(db, po)
            else:
                review.link_version(db, intake, version, snapshot, assessment, settings.reviewer)
        return intake_detail(intake_id)

    def decide_revision(intake_id, request, activate):
        action = "activated" if activate else "dismissed"
        with Session(engine) as db, db.begin():
            intake, version, snapshot, po = lock_review(db, intake_id)
            stale = check_expected(db, intake, version, snapshot, po, request, action)
            if stale is not None:
                return stale
            if not po or po.status == "ordered" or version.disposition != "pending":
                return conflict(intake, "This document is no longer a pending revision.")
            if activate:
                assessment = assess_snapshot(db, intake, snapshot, po)
                if review.complete_content(assessment) is None:
                    return conflict(intake, "Resolve the revision's evidence and required fields before activation.",
                                    assessment["issues"])
                db.get(Version, po.active_version_id).disposition = "superseded"
                version.disposition = "active"
                po.active_version_id, po.active_snapshot_id = version.id, snapshot.id
                review.record(db, version, snapshot, "validation", settings.reviewer, assessment)
                review.reconcile_pending(db, po, settings.reviewer)
            else:
                version.disposition = "dismissed"
            review.record(db, version, snapshot, action, settings.reviewer,
                          {"reason": request.reason.strip(), "previous_active": request.active_snapshot_id})
            review.update_status(db, po)
        return intake_detail(intake_id)

    @app.post("/intakes/{intake_id}/activate")
    def activate_revision(intake_id: int, request: ReviewRequest):
        return decide_revision(intake_id, request, activate=True)

    @app.post("/intakes/{intake_id}/dismiss")
    def dismiss_revision(intake_id: int, request: ReviewRequest):
        return decide_revision(intake_id, request, activate=False)

    @app.post("/intakes/{intake_id}/approve")
    def approve(intake_id: int, request: Approve):
        with Session(engine) as db, db.begin():
            intake, version, snapshot, po = lock_review(db, intake_id)
            if not snapshot:
                return conflict(intake, "Approval requires the original receipt and a current draft.")
            if po and po.status == "ordered" and version.id == po.active_version_id and request.snapshot_id == po.active_snapshot_id:
                return order_summary(db.scalar(select(SalesOrder).where(SalesOrder.po_id == po.id)))
            if not po or request.snapshot_id != snapshot.id or request.snapshot_id != po.active_snapshot_id:
                review.record(db, version, snapshot, "approval_blocked", settings.reviewer,
                              {"reason": "Snapshot is stale, unlinked, or inactive", "submitted": request.snapshot_id})
                return conflict(intake, "This snapshot is stale, unlinked, or inactive. Refresh to review the active snapshot.")
            assessment = assess_snapshot(db, intake, snapshot, po, lock_prices=True)
            review.record(db, version, snapshot, "validation", settings.reviewer, assessment)
            pending = review.blockers(db, po)
            issues = list(assessment["issues"])
            if pending:
                issues.append("Pending revisions block approval: " + ", ".join(f"V{number}" for number in pending))
            if issues:
                review.record(db, version, snapshot, "approval_blocked", settings.reviewer, {"issues": issues})
                review.update_status(db, po)
                return conflict(intake, "Approval blocked.", issues)
            validation = assessment["validation"]
            order = SalesOrder(
                customer=po.customer, po_key=po.po_key, po_number=validation["po_number"],
                currency=validation["currency"], intake_id=intake_id, po_id=po.id, version_id=version.id,
                snapshot_id=snapshot.id, reviewer=settings.reviewer,
            )
            db.add(order)
            db.flush()
            for position, line in enumerate(validation["lines"], start=1):
                db.add(OrderLine(
                    order_id=order.id, position=position, sku=line["sku"],
                    quantity=line["quantity"], unit_price=Decimal(line["unit_price"]),
                ))
            po.status = "ordered"
            review.record(db, version, snapshot, "approved", settings.reviewer, {"order_id": order.id})
            return order_summary(order)

    @app.get("/orders")
    def orders():
        with Session(engine) as db:
            return [order_summary(order) for order in db.scalars(select(SalesOrder).order_by(SalesOrder.id.desc()))]

    @app.get("/orders/{order_id}")
    def order_detail(order_id: int):
        with Session(engine) as db:
            order = db.get(SalesOrder, order_id)
            if order is None:
                raise HTTPException(404, "Order not found.")
            lines = db.scalars(select(OrderLine).where(OrderLine.order_id == order_id).order_by(OrderLine.position))
            return {
                **order_summary(order), "version": db.get(Version, order.version_id).number,
                "lines": [
                    {"sku": line.sku, "quantity": line.quantity, "unit_price": f"{line.unit_price:.2f}"}
                    for line in lines
                ],
            }

    return app


app = create_app()
