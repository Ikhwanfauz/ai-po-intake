from datetime import datetime
from typing import Literal

from pydantic import BaseModel


TOOL_NAME = "get_po_review_context"


class ActiveDocument(BaseModel):
    version: int
    intake_id: int
    snapshot_id: int
    edit: int


class Candidate(BaseModel):
    version: int
    intake_id: int
    disposition: str


class CheckedLine(BaseModel):
    position: int
    sku: str | None
    quantity: int | None
    checked_unit_price: str | None
    expected_unit_price: str | None
    price_verified: bool
    price_evidence_page: int | None
    price_excerpt: str | None


class ApprovedOrder(BaseModel):
    number: str
    snapshot_id: int


class ReviewContext(BaseModel):
    po_id: int
    po_number: str
    customer: str
    customer_reference: str | None
    currency: str
    status: Literal["needs_review", "ready_for_approval", "ordered"]
    active_document: ActiveDocument
    latest_candidate: Candidate | None
    pending_versions: list[int]
    issues: list[str]
    checked_lines: list[CheckedLine]
    checked_at: datetime
    read_at: datetime
    approved_order: ApprovedOrder | None = None
