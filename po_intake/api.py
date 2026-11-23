import logging
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import PureWindowsPath

import pymupdf
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from po_intake.config import Settings
from po_intake.db import Attempt, Customer, Document, Intake, OrderLine, SalesOrder, make_engine
from po_intake.extraction import (
    MAX_PDF_BYTES, MAX_TEXT_CHARS, MODEL_OPTIONS, Draft, extract_with_ollama,
    open_pdf, pdf_pages, store_pdf,
)
from po_intake.validation import validate

log = logging.getLogger(__name__)


class Approve(BaseModel):
    snapshot_id: int = Field(gt=0)


class Interrupt(BaseModel):
    attempt_id: int = Field(gt=0)


def get_intake(db, intake_id, lock=False):
    intake = db.get(Intake, intake_id, with_for_update=lock)
    if intake is None:
        raise HTTPException(404, "Intake not found.")
    return intake


def latest_attempt(db, intake_id):
    return db.scalar(
        select(Attempt).where(Attempt.intake_id == intake_id).order_by(Attempt.id.desc()).limit(1)
    )


def intake_summary(intake):
    return {
        "id": intake.id, "filename": intake.filename, "state": intake.state,
        "received_at": intake.received_at, "customer_context": intake.customer_context,
    }


def order_summary(order):
    return {
        "id": order.id, "number": f"SO-{order.id:06d}", "po_number": order.po_number,
        "customer": order.customer, "snapshot_id": order.snapshot_id, "intake_id": order.intake_id,
        "currency": order.currency, "reviewer": order.reviewer, "approved_at": order.approved_at,
    }


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
            if customer_context and db.get(Customer, customer_context) is None:
                raise HTTPException(422, "Selected customer does not exist.")
            try:
                digest, path = store_pdf(settings.storage, data)
            except OSError as exc:
                log.exception("Could not store original PDF")
                raise HTTPException(503, "Could not store the original PDF.") from exc
            db.execute(insert(Document).values(
                digest=digest, path=path, size=len(data)
            ).on_conflict_do_nothing())
            intake = Intake(
                digest=digest, filename=PureWindowsPath(file.filename or "order.pdf").name,
                customer_context=customer_context or None,
            )
            db.add(intake)
            db.flush()
            return intake_summary(intake)

    @app.get("/intakes")
    def intakes():
        with Session(engine) as db:
            return [intake_summary(intake) for intake in db.scalars(select(Intake).order_by(Intake.id.desc()))]

    @app.get("/intakes/{intake_id}")
    def intake_detail(intake_id: int):
        with Session(engine) as db:
            intake = get_intake(db, intake_id)
            result = intake_summary(intake)
            attempt = latest_attempt(db, intake_id)
            result["attempt"] = None
            result["order"] = None
            if attempt:
                result["attempt"] = {
                    "id": attempt.id, "state": attempt.state, "started_at": attempt.started_at,
                    "finished_at": attempt.finished_at, "settings": attempt.settings, "error": attempt.error,
                    "draft": attempt.draft, "validation": attempt.validation, "pages": attempt.pages,
                }
                order = db.scalar(select(SalesOrder).where(SalesOrder.snapshot_id == attempt.id))
                if order:
                    result["order"] = order_summary(order)
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
            intake = get_intake(db, intake_id, lock=True)
            if intake.state not in {"pending", "failed"}:
                raise HTTPException(409, f"Intake is already {intake.state}. Refresh its review.")
            path = settings.storage / db.get(Document, intake.digest).path
            attempt = Attempt(
                intake_id=intake.id, state="processing",
                settings={"model": settings.model, "think": False, "timeout": settings.timeout, **MODEL_OPTIONS},
            )
            intake.state = "processing"
            db.add(attempt)
            db.flush()
            attempt_id = attempt.id

        pages = raw = draft = error = None
        try:
            pages = pdf_pages(path.read_bytes())
            if sum(len(page["text"]) for page in pages) > MAX_TEXT_CHARS:
                raise ValueError("The PDF exceeds the local extraction text limit.")
            raw = extractor(settings, pages) if any(page["text"].strip() for page in pages) else None
            draft = Draft.model_validate_json(raw) if raw is not None else Draft()
        except ValidationError:
            error = "Ollama returned a response that does not match the extraction schema."
        except (ValueError, OSError, pymupdf.FileDataError) as exc:
            error = str(exc)
        except Exception:
            log.exception("Extraction failed for intake %s", intake_id)
            error = "Extraction failed unexpectedly. Check the API log and retry."

        with Session(engine) as db, db.begin():
            intake = get_intake(db, intake_id, lock=True)
            attempt = latest_attempt(db, intake_id)
            if attempt.id != attempt_id or attempt.state != "processing":
                raise HTTPException(409, "This extraction attempt is no longer current.")
            attempt.pages = pages
            attempt.raw = raw
            attempt.error = error
            attempt.finished_at = datetime.now(timezone.utc)
            if error is not None:
                attempt.state = intake.state = "failed"
            else:
                attempt.draft = draft.model_dump(mode="json")
                attempt.validation = validate(db, draft, pages, intake.customer_context)
                attempt.state = intake.state = "complete"
        return intake_detail(intake_id)

    @app.post("/intakes/{intake_id}/interrupt")
    def interrupt(intake_id: int, request: Interrupt):
        with Session(engine) as db, db.begin():
            intake = get_intake(db, intake_id, lock=True)
            attempt = latest_attempt(db, intake_id)
            if intake.state != "processing" or attempt.id != request.attempt_id:
                raise HTTPException(409, "The processing attempt changed. Refresh its review.")
            attempt.state = intake.state = "failed"
            attempt.error = f"Attempt marked interrupted by {settings.reviewer}."
            attempt.finished_at = datetime.now(timezone.utc)
        return intake_detail(intake_id)

    @app.post("/intakes/{intake_id}/validate")
    def revalidate(intake_id: int, request: Approve):
        with Session(engine) as db, db.begin():
            intake = get_intake(db, intake_id, lock=True)
            attempt = latest_attempt(db, intake_id)
            if intake.state != "complete" or attempt.id != request.snapshot_id:
                raise HTTPException(409, "Snapshot is unavailable or changed. Refresh its review.")
            if db.scalar(select(SalesOrder.id).where(SalesOrder.snapshot_id == attempt.id)):
                raise HTTPException(409, "The snapshot already produced a sales order.")
            attempt.validation = validate(
                db, Draft.model_validate(attempt.draft), attempt.pages, intake.customer_context
            )
            return attempt.validation

    @app.post("/intakes/{intake_id}/approve")
    def approve(intake_id: int, request: Approve):
        with Session(engine) as db, db.begin():
            intake = get_intake(db, intake_id, lock=True)
            attempt = latest_attempt(db, intake_id)
            if intake.state != "complete" or attempt.id != request.snapshot_id:
                raise HTTPException(409, "Snapshot is unavailable or changed. Refresh its review.")
            existing = db.scalar(select(SalesOrder).where(SalesOrder.snapshot_id == attempt.id))
            if existing:
                return order_summary(existing)

            draft = Draft.model_validate(attempt.draft)
            customer = attempt.validation["customer"]
            if customer:
                # Serialize business-key approval across receipts for this customer.
                db.get(Customer, customer, with_for_update=True)
            validation = validate(db, draft, attempt.pages, intake.customer_context, lock_prices=True)
            attempt.validation = validation
            issues = validation["issues"]
            if not issues:
                po_number = validation["po_number"]
                po_key = po_number.casefold()
                duplicate = db.scalar(select(SalesOrder).where(
                    SalesOrder.customer == validation["customer"], SalesOrder.po_key == po_key,
                ))
                if duplicate:
                    raise HTTPException(409, f"This customer PO already produced {order_summary(duplicate)['number']}.")
                order = SalesOrder(
                    customer=validation["customer"], po_key=po_key, po_number=po_number,
                    currency=validation["currency"], intake_id=intake_id,
                    snapshot_id=attempt.id, reviewer=settings.reviewer,
                )
                db.add(order)
                db.flush()
                for position, line in enumerate(validation["lines"], start=1):
                    db.add(OrderLine(
                        order_id=order.id, position=position, sku=line["sku"],
                        quantity=line["quantity"], unit_price=Decimal(line["unit_price"]),
                    ))
                return order_summary(order)
        raise HTTPException(409, {"message": "Approval blocked.", "issues": issues})

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
                **order_summary(order),
                "lines": [
                    {"sku": line.sku, "quantity": line.quantity, "unit_price": f"{line.unit_price:.2f}"}
                    for line in lines
                ],
            }

    return app


app = create_app()
