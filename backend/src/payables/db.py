"""Database schema (SQLAlchemy 2.0). Migrations in alembic/ create the same tables, the triggers and the
database roles: tables belong to payables_owner (no login); the API and the agent connect as
payables_api and payables_agent, which can neither change the schema nor disable a trigger.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, ClassVar

from sqlalchemy import (
    BigInteger,
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

#: Database roles (created by the migrations).
OWNER_ROLE = "payables_owner"
API_ROLE = "payables_api"
AGENT_ROLE = "payables_agent"


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(60), unique=True)
    display_name: Mapped[str] = mapped_column(String(120))
    role: Mapped[Role] = mapped_column(Enum(Role, name="role"))
    #: Matches the From: address of emailed invoices, so an approver's own submissions are known.
    email: Mapped[str | None] = mapped_column(String(200), unique=True)
    password_hash: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class SessionRow(Base):
    """Server-side sessions: the cookie carries a random token; signing out revokes it here."""

    __tablename__ = "sessions"
    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    expires_at: Mapped[datetime]
    revoked_at: Mapped[datetime | None]


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
    #: Contact numbers on file: the only numbers a bank-change callback may use.
    phones: Mapped[list[str]] = mapped_column(JSONB, server_default="[]")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class DocumentRow(Base):
    __tablename__ = "documents"
    id: Mapped[int] = mapped_column(primary_key=True)
    #: Hash of what the model reads (images are normalised first); also the idempotency key.
    sha256: Mapped[str] = mapped_column(String(64), unique=True)
    filename: Mapped[str] = mapped_column(String(160))
    #: The original upload, byte for byte, and its sniffed type: what "download" returns.
    media_type: Mapped[str] = mapped_column(String(40))
    content: Mapped[bytes] = mapped_column(LargeBinary)
    original_sha256: Mapped[str] = mapped_column(String(64))
    #: Normalised image sent to the model (None for PDFs, which are sent as uploaded).
    normalized: Mapped[bytes | None] = mapped_column(LargeBinary)
    #: First page rendered once at ingest, size-capped: the browser never renders an upload.
    preview_png: Mapped[bytes] = mapped_column(LargeBinary)
    source: Mapped[str] = mapped_column(String(20))
    message_text: Mapped[str | None] = mapped_column(Text)
    uploaded_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    #: Who sent it when it came by email (the From: address; spoofable, shown as a hint).
    submitted_by_email: Mapped[str | None] = mapped_column(String(200))
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
    currency: Mapped[str] = mapped_column(String(3), server_default="ILS")
    issue_date: Mapped[date | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class DraftRow(Base):
    __tablename__ = "payment_drafts"
    __table_args__ = (
        # Separation of duties, enforced by the database: whoever prepared a draft cannot decide it.
        CheckConstraint("decided_by IS NULL OR decided_by <> prepared_by", name="preparer_cannot_decide"),
        # An approved bank-change draft carries a structured phone callback.
        CheckConstraint(
            "state <> 'APPROVED' OR NOT requires_bank_verification OR ("
            "btrim(coalesce(callback_phone, '')) <> '' AND btrim(coalesce(callback_person, '')) <> '' "
            "AND callback_at IS NOT NULL)",
            name="bank_change_needs_callback",
        ),
        CheckConstraint("amount IS NULL OR amount > 0", name="draft_amount_positive"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id"), unique=True)
    state: Mapped[DraftState] = mapped_column(Enum(DraftState, name="draft_state"))
    amount: Mapped[Decimal | None] = mapped_column(Money)
    currency: Mapped[str] = mapped_column(String(3), server_default="ILS")
    checks: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    flags: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    reasoning: Mapped[list[str]] = mapped_column(JSONB)
    requires_bank_verification: Mapped[bool]
    pay_to: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    prepared_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    decided_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    decided_at: Mapped[datetime | None]
    decision_comment: Mapped[str | None] = mapped_column(Text)
    #: The bank-change callback: a number from the vendor master, who confirmed, and when.
    callback_phone: Mapped[str | None] = mapped_column(String(40))
    callback_person: Mapped[str | None] = mapped_column(String(120))
    callback_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class SimulatedPayment(Base):
    """A payment that would have been sent. Nothing in this project moves money."""

    __tablename__ = "simulated_payments"
    __table_args__ = (
        UniqueConstraint("draft_id"),
        CheckConstraint("amount > 0", name="payment_amount_positive"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    draft_id: Mapped[int] = mapped_column(ForeignKey("payment_drafts.id"))
    amount: Mapped[Decimal] = mapped_column(Money)
    currency: Mapped[str] = mapped_column(String(3), server_default="ILS")
    pay_to: Mapped[dict[str, Any]] = mapped_column(JSONB)
    reference: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class AuditEntry(Base):
    """Append-only (a trigger rejects UPDATE, DELETE and TRUNCATE) and HMAC-chained (audit.py)."""

    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    #: 1, 2, 3, ... with no gaps: a missing number is a deleted entry.
    seq: Mapped[int] = mapped_column(BigInteger, unique=True)
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
    # Each request holds at most one connection at a time; a short pool timeout turns exhaustion
    # into a quick 503 rather than a 30-second hang.
    return create_engine(url, pool_pre_ping=True, pool_size=10, max_overflow=10, pool_timeout=10)


def make_sessionmaker(engine: Engine) -> sessionmaker[Any]:
    return sessionmaker(engine, expire_on_commit=False)
