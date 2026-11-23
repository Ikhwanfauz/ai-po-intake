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
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    digest: Mapped[str] = mapped_column(ForeignKey("documents.digest"))
    filename: Mapped[str]
    source: Mapped[str] = mapped_column(server_default="upload")
    customer_context: Mapped[str | None] = mapped_column(ForeignKey("customers.reference"))
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


class SalesOrder(Base):
    __tablename__ = "sales_orders"
    __table_args__ = (
        UniqueConstraint("customer", "po_key", name="one_order_per_customer_po"),
        ForeignKeyConstraint(["intake_id", "snapshot_id"], ["attempts.intake_id", "attempts.id"]),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    customer: Mapped[str] = mapped_column(ForeignKey("customers.reference"))
    po_key: Mapped[str]
    po_number: Mapped[str]
    currency: Mapped[str]
    intake_id: Mapped[int]
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
