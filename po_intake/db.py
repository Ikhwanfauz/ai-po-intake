from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    JSON, CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, Index,
    Numeric, UniqueConstraint, create_engine, func, text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def make_engine(url):
    return create_engine(url, pool_pre_ping=True)


class Base(DeclarativeBase):
    pass


class Customer(Base):
    __tablename__ = "customers"
    reference: Mapped[str] = mapped_column(primary_key=True)
    name: Mapped[str]


class CustomerAlias(Base):
    __tablename__ = "customer_aliases"
    alias: Mapped[str] = mapped_column(primary_key=True)
    customer: Mapped[str] = mapped_column(ForeignKey("customers.reference"))


class SKU(Base):
    __tablename__ = "skus"
    code: Mapped[str] = mapped_column(primary_key=True)
    description: Mapped[str]


class SKUAlias(Base):
    __tablename__ = "sku_aliases"
    alias: Mapped[str] = mapped_column(primary_key=True)
    sku: Mapped[str] = mapped_column(ForeignKey("skus.code"))


class Price(Base):
    __tablename__ = "prices"
    __table_args__ = (CheckConstraint("amount >= 0"),)
    customer: Mapped[str] = mapped_column(ForeignKey("customers.reference"), primary_key=True)
    sku: Mapped[str] = mapped_column(ForeignKey("skus.code"), primary_key=True)
    currency: Mapped[str] = mapped_column(primary_key=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))


class Document(Base):
    __tablename__ = "documents"
    digest: Mapped[str] = mapped_column(primary_key=True)
    path: Mapped[str] = mapped_column(unique=True)
    size: Mapped[int]


class Intake(Base):
    __tablename__ = "intakes"
    __table_args__ = (
        CheckConstraint("state IN ('pending', 'processing', 'complete', 'failed')"),
        UniqueConstraint("source", "event_key", name="unique_intake_event"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    digest: Mapped[str] = mapped_column(ForeignKey("documents.digest"))
    filename: Mapped[str]
    source: Mapped[str] = mapped_column(server_default="upload")
    customer_context: Mapped[str | None] = mapped_column(ForeignKey("customers.reference"))
    canonical_id: Mapped[int | None] = mapped_column(ForeignKey("intakes.id"), index=True)
    context_issue: Mapped[str | None]
    event_key: Mapped[str | None]
    sender: Mapped[str | None]
    gmail_message_id: Mapped[str | None]
    gmail_attachment_id: Mapped[str | None]
    state: Mapped[str] = mapped_column(server_default="pending")
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Attempt(Base):
    __tablename__ = "attempts"
    __table_args__ = (
        CheckConstraint("state IN ('processing', 'complete', 'failed')"),
        UniqueConstraint("intake_id", "id"),
        Index("one_processing_attempt", "intake_id", "pipeline", unique=True,
              postgresql_where=text("state = 'processing'")),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    intake_id: Mapped[int] = mapped_column(ForeignKey("intakes.id"), index=True)
    pipeline: Mapped[str] = mapped_column(server_default="baseline")
    state: Mapped[str]
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    settings: Mapped[dict] = mapped_column(JSON)
    pages: Mapped[list | None] = mapped_column(JSON)
    raw: Mapped[str | None]
    draft: Mapped[dict | None] = mapped_column(JSON)
    validation: Mapped[dict | None] = mapped_column(JSON)
    error: Mapped[str | None]


class PurchaseOrder(Base):
    __tablename__ = "purchase_orders"
    __table_args__ = (
        UniqueConstraint("customer", "po_key", name="unique_purchase_order_key"),
        ForeignKeyConstraint(["id", "active_version_id"], ["po_versions.po_id", "po_versions.id"],
                             name="fk_po_active_version", use_alter=True),
        ForeignKeyConstraint(["active_version_id", "active_snapshot_id"],
                             ["draft_snapshots.version_id", "draft_snapshots.id"],
                             name="fk_po_active_snapshot", use_alter=True),
        CheckConstraint("(active_version_id IS NULL) = (active_snapshot_id IS NULL)"),
        CheckConstraint("status IN ('needs_review', 'ready_for_approval', 'ordered')"),
    )
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement="ignore_fk")
    customer: Mapped[str] = mapped_column(ForeignKey("customers.reference"))
    po_key: Mapped[str]
    status: Mapped[str]
    active_version_id: Mapped[int | None]
    active_snapshot_id: Mapped[int | None]


class Version(Base):
    __tablename__ = "po_versions"
    __table_args__ = (
        UniqueConstraint("po_id", "number"),
        UniqueConstraint("po_id", "id"),
        UniqueConstraint("intake_id", "id"),
        CheckConstraint("(po_id IS NULL) = (number IS NULL)"),
        CheckConstraint("disposition IN ('unlinked', 'active', 'pending', 'equivalent', "
                        "'superseded', 'dismissed', 'post_order', 'exact_resend')"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    intake_id: Mapped[int] = mapped_column(ForeignKey("intakes.id"), unique=True)
    po_id: Mapped[int | None] = mapped_column(ForeignKey("purchase_orders.id"))
    number: Mapped[int | None]
    disposition: Mapped[str]


class Snapshot(Base):
    __tablename__ = "draft_snapshots"
    __table_args__ = (
        UniqueConstraint("version_id", "id"),
        UniqueConstraint("version_id", "edit"),
        ForeignKeyConstraint(["version_id", "parent_id"], ["draft_snapshots.version_id", "draft_snapshots.id"]),
    )
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    version_id: Mapped[int] = mapped_column(ForeignKey("po_versions.id"))
    attempt_id: Mapped[int] = mapped_column(ForeignKey("attempts.id"))
    parent_id: Mapped[int | None]
    edit: Mapped[int]
    author: Mapped[str]
    reason: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    draft: Mapped[dict] = mapped_column(JSON)
    assessment: Mapped[dict | None] = mapped_column(JSON)


class ReviewEvent(Base):
    __tablename__ = "review_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    version_id: Mapped[int] = mapped_column(ForeignKey("po_versions.id"), index=True)
    snapshot_id: Mapped[int | None] = mapped_column(ForeignKey("draft_snapshots.id"))
    action: Mapped[str]
    author: Mapped[str]
    detail: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SalesOrder(Base):
    __tablename__ = "sales_orders"
    __table_args__ = (
        UniqueConstraint("customer", "po_key", name="one_order_per_customer_po"),
        ForeignKeyConstraint(["po_id", "version_id"], ["po_versions.po_id", "po_versions.id"],
                             name="fk_order_po_version"),
        ForeignKeyConstraint(["intake_id", "version_id"], ["po_versions.intake_id", "po_versions.id"],
                             name="fk_order_intake_version"),
        ForeignKeyConstraint(["version_id", "snapshot_id"], ["draft_snapshots.version_id", "draft_snapshots.id"],
                             name="fk_order_review_snapshot"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    customer: Mapped[str] = mapped_column(ForeignKey("customers.reference"))
    po_key: Mapped[str]
    po_number: Mapped[str]
    currency: Mapped[str]
    intake_id: Mapped[int]
    po_id: Mapped[int] = mapped_column(ForeignKey("purchase_orders.id"), unique=True)
    version_id: Mapped[int]
    snapshot_id: Mapped[int] = mapped_column(unique=True)
    reviewer: Mapped[str]
    approved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OrderLine(Base):
    __tablename__ = "order_lines"
    __table_args__ = (CheckConstraint("quantity > 0"), CheckConstraint("unit_price >= 0"))
    order_id: Mapped[int] = mapped_column(ForeignKey("sales_orders.id"), primary_key=True)
    position: Mapped[int] = mapped_column(primary_key=True)
    sku: Mapped[str] = mapped_column(ForeignKey("skus.code"))
    quantity: Mapped[int]
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2))

class ExtractionJob(Base):
    __tablename__ = "extraction_jobs"
    __table_args__ = (
        CheckConstraint("state IN ('pending', 'running', 'complete', 'failed')"),
        CheckConstraint("(state = 'running') = (claim_token IS NOT NULL AND lease_until IS NOT NULL AND attempt_id IS NOT NULL)"),
        Index("ix_extraction_jobs_ready", "state", "available_at"),
    )
    intake_id: Mapped[int] = mapped_column(ForeignKey("intakes.id"), primary_key=True)
    state: Mapped[str] = mapped_column(server_default="pending")
    attempt_count: Mapped[int] = mapped_column(server_default="0")
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    claim_token: Mapped[str | None]
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_id: Mapped[int | None] = mapped_column(ForeignKey("attempts.id"))


class Notification(Base):
    __tablename__ = "notification_intents"
    __table_args__ = (
        CheckConstraint("state IN ('pending', 'sending', 'sent', 'failed', 'unknown')"),
        CheckConstraint("channel IN ('telegram', 'gmail')"),
        Index("ix_notification_ready", "state", "available_at"),
        Index("one_open_review_episode", "episode_scope", unique=True,
              postgresql_where=text("episode_scope IS NOT NULL AND closed_at IS NULL")),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    event_key: Mapped[str] = mapped_column(unique=True)
    intake_id: Mapped[int] = mapped_column(ForeignKey("intakes.id"), index=True)
    po_id: Mapped[int | None] = mapped_column(ForeignKey("purchase_orders.id"), index=True)
    order_id: Mapped[int | None] = mapped_column(ForeignKey("sales_orders.id"), unique=True)
    episode_scope: Mapped[str | None]
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    channel: Mapped[str]
    destination: Mapped[str | None]
    destination_alias: Mapped[str]
    payload: Mapped[dict] = mapped_column(JSON)
    state: Mapped[str] = mapped_column(server_default="pending")
    attempt_count: Mapped[int] = mapped_column(server_default="0")
    current_attempt_id: Mapped[str | None]
    retryable: Mapped[bool] = mapped_column(server_default=text("false"))
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class NotificationAttempt(Base):
    __tablename__ = "notification_attempts"
    __table_args__ = (
        CheckConstraint("outcome IN ('sending', 'sent', 'failed', 'unknown')"),
        UniqueConstraint("notification_id", "number"),
    )
    id: Mapped[str] = mapped_column(primary_key=True)
    notification_id: Mapped[int] = mapped_column(ForeignKey("notification_intents.id"))
    number: Mapped[int]
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outcome: Mapped[str]
    result: Mapped[dict | None] = mapped_column(JSON)
    reconciliation: Mapped[dict | None] = mapped_column(JSON)
    retry_authorization: Mapped[dict | None] = mapped_column(JSON)
