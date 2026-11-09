"""Database schema (SQLAlchemy 2.0). Migrations in alembic/ create the same tables plus the audit trigger."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, ClassVar

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


class Base(DeclarativeBase):
    # All timestamps are timezone-aware (timestamptz): the audit hash covers them exactly.
    type_annotation_map: ClassVar[dict[Any, Any]] = {datetime: DateTime(timezone=True)}


class Role(StrEnum):
    AGENT = "AGENT"
    APPROVER = "APPROVER"
    ADMIN = "ADMIN"


class DraftState(StrEnum):
    READY = "READY"
    NEEDS_ATTENTION = "NEEDS_ATTENTION"
    BLOCKED = "BLOCKED"
    EXTRACTION_FAILED = "EXTRACTION_FAILED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


Money = Numeric(14, 2)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(60), unique=True)
    display_name: Mapped[str] = mapped_column(String(120))
    role: Mapped[Role] = mapped_column(Enum(Role, name="role"))
    password_hash: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class VendorRow(Base):
    __tablename__ = "vendors"
    id: Mapped[int] = mapped_column(primary_key=True)
    tax_id: Mapped[str] = mapped_column(String(9), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    bank_name: Mapped[str | None] = mapped_column(String(80))
    bank_code: Mapped[str | None] = mapped_column(String(3))
    branch: Mapped[str | None] = mapped_column(String(4))
    account: Mapped[str | None] = mapped_column(String(20))
    iban: Mapped[str | None] = mapped_column(String(34))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class DocumentRow(Base):
    __tablename__ = "documents"
    id: Mapped[int] = mapped_column(primary_key=True)
    sha256: Mapped[str] = mapped_column(String(64), unique=True)
    filename: Mapped[str] = mapped_column(String(160))
    media_type: Mapped[str] = mapped_column(String(40))
    content: Mapped[bytes] = mapped_column(LargeBinary)
    source: Mapped[str] = mapped_column(String(20))
    message_text: Mapped[str | None] = mapped_column(Text)
    uploaded_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    received_at: Mapped[datetime] = mapped_column(server_default=func.now())


class InvoiceRow(Base):
    __tablename__ = "invoices"
    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), unique=True)
    extracted: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    extraction_error: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str] = mapped_column(String(80))
    vendor_tax_id: Mapped[str | None] = mapped_column(String(9), index=True)
    invoice_number: Mapped[str | None] = mapped_column(String(60))
    total: Mapped[Decimal | None] = mapped_column(Money)
    issue_date: Mapped[date | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class DraftRow(Base):
    __tablename__ = "payment_drafts"
    __table_args__ = (
        # Separation of duties, enforced by the database: whoever prepared a draft cannot decide it.
        CheckConstraint("decided_by IS NULL OR decided_by <> prepared_by", name="preparer_cannot_decide"),
        CheckConstraint(
            "state <> 'APPROVED' OR NOT requires_bank_verification OR bank_verification_note IS NOT NULL",
            name="bank_change_needs_verification",
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id"), unique=True)
    state: Mapped[DraftState] = mapped_column(Enum(DraftState, name="draft_state"))
    amount: Mapped[Decimal | None] = mapped_column(Money)
    checks: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    flags: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    reasoning: Mapped[list[str]] = mapped_column(JSONB)
    requires_bank_verification: Mapped[bool]
    pay_to: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    prepared_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    decided_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    decided_at: Mapped[datetime | None]
    decision_comment: Mapped[str | None] = mapped_column(Text)
    bank_verification_note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class SimulatedPayment(Base):
    """A payment that would have been sent. Nothing in this project moves money."""

    __tablename__ = "simulated_payments"
    __table_args__ = (UniqueConstraint("draft_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    draft_id: Mapped[int] = mapped_column(ForeignKey("payment_drafts.id"))
    amount: Mapped[Decimal] = mapped_column(Money)
    pay_to: Mapped[dict[str, Any]] = mapped_column(JSONB)
    reference: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class AuditEntry(Base):
    """Append-only (a trigger rejects UPDATE and DELETE) and hash-chained (each row hashes the previous)."""

    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(server_default=func.now())
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    actor: Mapped[str] = mapped_column(String(60))
    action: Mapped[str] = mapped_column(String(60))
    entity: Mapped[str] = mapped_column(String(40))
    entity_id: Mapped[int | None]
    details: Mapped[dict[str, Any]] = mapped_column(JSONB)
    prev_hash: Mapped[str] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(64), unique=True)


def make_engine(url: str) -> Engine:
    return create_engine(url, pool_pre_ping=True)


def make_sessionmaker(engine: Engine) -> sessionmaker[Any]:
    return sessionmaker(engine, expire_on_commit=False)
