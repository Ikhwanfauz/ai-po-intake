import base64
import binascii
import hashlib
import json
from email.utils import parseaddr
from pathlib import PureWindowsPath

import pymupdf
from fastapi import HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from po_intake import jobs, review
from po_intake.config import EMAIL_ADDRESS
from po_intake.db import Customer, Document, Intake
from po_intake.extraction import MAX_PDF_BYTES, open_pdf, store_pdf


class GmailAttachment(BaseModel):
    message_id: str = Field(min_length=1, max_length=256, pattern=r"^[!-~]+$")
    attachment_id: str = Field(min_length=1, max_length=1500, pattern=r"^[!-~]+$")
    sender: str = Field(default="", max_length=1000)
    filename: str = Field(default="attachment.pdf", max_length=256)
    pdf_base64: str = Field(min_length=1, max_length=4 * ((MAX_PDF_BYTES + 2) // 3))

    def pdf_bytes(self):
        try:
            return base64.b64decode(self.pdf_base64 + "=" * (-len(self.pdf_base64) % 4),
                                    altchars=b"-_", validate=True)
        except (ValueError, binascii.Error) as exc:
            raise HTTPException(422, "Invalid Gmail attachment encoding.") from exc


def accept(db, settings, pdf, filename, context=None, gmail=None):
    source = "gmail" if gmail else "upload"
    event_key = sender = context_issue = None
    if gmail:
        encoded_key = json.dumps([gmail.message_id, gmail.attachment_id], separators=(",", ":")).encode()
        digest_key = hashlib.sha256(encoded_key).digest()
        event_key = digest_key.hex()
        # Serialize the first write even if two requests disagree about the attachment bytes.
        db.execute(select(func.pg_advisory_xact_lock(int.from_bytes(digest_key[:8], "big", signed=True))))
        existing = db.scalar(select(Intake).where(Intake.source == source, Intake.event_key == event_key))
        if existing:
            work = db.get(Intake, existing.canonical_id) if existing.canonical_id else existing
            return existing, work, False
        address = parseaddr(gmail.sender)[1].strip().lower()
        sender = address if EMAIL_ADDRESS.fullmatch(address) else None
        context = settings.gmail_senders.get(sender)
        if context is None:
            context_issue = "The Gmail sender is not mapped to a demo customer. Verify the sender before linking."
        pdf = gmail.pdf_bytes()
        filename = gmail.filename

    if not pdf or len(pdf) > MAX_PDF_BYTES:
        raise HTTPException(422, "Upload a nonempty PDF of at most 10 MiB.")
    try:
        with open_pdf(pdf):
            pass
    except (ValueError, pymupdf.FileDataError) as exc:
        raise HTTPException(422, "Upload a readable, unencrypted PDF with 1 to 20 pages.") from exc
    if context and db.get(Customer, context) is None:
        raise HTTPException(422, "The selected or mapped customer does not exist.")
    try:
        digest, path = store_pdf(settings.storage, pdf)
    except OSError as exc:
        raise HTTPException(503, "Could not store the original PDF.") from exc
    db.execute(insert(Document).values(digest=digest, path=path, size=len(pdf)).on_conflict_do_nothing())
    db.get(Document, digest, with_for_update=True)
    canonical = None
    if not context_issue:
        canonical, context_issue = review.exact_delivery(db, digest, context)
    name = PureWindowsPath((filename or "order.pdf").replace("\x00", "")).name
    receipt = Intake(
        digest=digest, filename=name or "order.pdf", source=source,
        customer_context=context, canonical_id=canonical.id if canonical else None,
        context_issue=context_issue, event_key=event_key, sender=sender,
        gmail_message_id=gmail.message_id if gmail else None,
        gmail_attachment_id=gmail.attachment_id if gmail else None,
    )
    db.add(receipt)
    db.flush()
    if canonical is None:
        jobs.enqueue(db, receipt.id)
    return receipt, canonical or receipt, True
