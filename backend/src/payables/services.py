"""The workflow: receive a document, extract, check, draft; then a person approves or rejects.

The agent (a user with the AGENT role and no password) prepares every draft. Only an APPROVER can
decide, never the agent, and never the person who uploaded the document. The database enforces the
first rule too. Every step is written to the audit log in the same transaction as the change.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from payables import audit, tax_id
from payables.db import DocumentRow, DraftRow, DraftState, InvoiceRow, Role, SimulatedPayment, User, VendorRow
from payables.drafting import build_draft
from payables.extraction import documents
from payables.extraction.claude import Extractor
from payables.extraction.model import ExtractionError, parse
from payables.risk import PastInvoice, RiskContext, Vendor, assess
from payables.schemas import BankDetails, ExtractedInvoice
from payables.validation import run_checks

AGENT_USERNAME = "agent"
MIN_VERIFICATION_NOTE = 15
_hasher = PasswordHasher()


class Forbidden(Exception):  # noqa: N818 - mirrors the HTTP status it maps to
    """The user may not do this (role or separation of duties)."""


class Conflict(Exception):  # noqa: N818
    """The draft is not in a state that allows this."""


class NotFound(Exception):  # noqa: N818
    pass


@dataclass(frozen=True)
class Received:
    draft_id: int
    duplicate_upload: bool


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def check_password(user: User, password: str) -> bool:
    if not user.password_hash:
        return False
    try:
        return _hasher.verify(user.password_hash, password)
    except VerifyMismatchError:
        return False


def vendor_from_row(v: VendorRow) -> Vendor:
    bank = None
    if v.iban or v.account:
        bank = BankDetails(
            bank_name=v.bank_name, bank_code=v.bank_code, branch=v.branch, account=v.account, iban=v.iban
        )
    return Vendor(tax_id=v.tax_id, name=v.name, bank=bank)


class Payables:
    def __init__(
        self, sessions: sessionmaker[Session], extractor: Extractor, today: date | None = None
    ) -> None:
        self.sessions = sessions
        self.extractor = extractor
        self.fixed_today = today

    def today(self) -> date:
        return self.fixed_today or datetime.now(UTC).date()

    # ---- users and vendors -------------------------------------------------------------------

    def ensure_users(self, approver_password: str, admin_password: str) -> None:
        """Creates the demo users if missing. Safe when several processes start at once."""
        wanted = [
            (AGENT_USERNAME, "Payables agent", Role.AGENT, None),
            ("approver", "Dana (approver)", Role.APPROVER, approver_password),
            ("admin", "Avi (admin)", Role.ADMIN, admin_password),
        ]
        with self.sessions.begin() as s:
            for username, name, role, password in wanted:
                s.execute(
                    pg_insert(User)
                    .values(
                        username=username,
                        display_name=name,
                        role=role,
                        password_hash=hash_password(password) if password else None,
                    )
                    .on_conflict_do_nothing(index_elements=["username"])
                )

    def agent(self, s: Session) -> User:
        user = s.scalars(select(User).where(User.username == AGENT_USERNAME)).one()
        return user

    def upsert_vendor(self, actor: User, tax: str, name: str, bank: BankDetails | None) -> int:
        if actor.role is not Role.ADMIN:
            raise Forbidden("only an admin can change the vendor master")
        normalized = tax_id.normalize(tax)
        if not tax_id.is_valid(normalized):
            raise ValueError("invalid Israeli tax ID")
        with self.sessions.begin() as s:
            row = s.scalars(select(VendorRow).where(VendorRow.tax_id == normalized)).first()
            before = vendor_from_row(row).bank if row else None
            if row is None:
                row = VendorRow(tax_id=normalized, name=name)
                s.add(row)
            row.name = name
            if bank is not None:
                row.bank_name, row.bank_code, row.branch = bank.bank_name, bank.bank_code, bank.branch
                row.account, row.iban = bank.account, bank.iban
            s.flush()
            audit.append(
                s,
                actor,
                "vendor.saved",
                "vendor",
                row.id,
                {
                    "tax_id": normalized,
                    "name": name,
                    "bank_before": before.model_dump() if before else None,
                    "bank_after": bank.model_dump() if bank else None,
                },
            )
            return row.id

    # ---- receiving documents -----------------------------------------------------------------

    def receive(
        self,
        content: bytes,
        filename: str,
        source: str,
        uploaded_by: User | None,
        message_text: str | None = None,
    ) -> Received:
        """Stores the document, extracts it, runs the checks and prepares a draft. Idempotent per file."""
        doc = documents.load(content, filename)  # raises RejectedDocumentError
        with self.sessions.begin() as s:
            existing = s.scalars(select(DocumentRow).where(DocumentRow.sha256 == doc.sha256)).first()
            if existing is not None:
                draft = s.scalars(
                    select(DraftRow).join(InvoiceRow).where(InvoiceRow.document_id == existing.id)
                ).one()
                audit.append(
                    s,
                    uploaded_by,
                    "document.duplicate_upload",
                    "document",
                    existing.id,
                    {"filename": doc.filename},
                )
                return Received(draft.id, duplicate_upload=True)
            row = DocumentRow(
                sha256=doc.sha256,
                filename=doc.filename,
                media_type=doc.media_type,
                content=doc.content,
                source=source,
                message_text=message_text,
                uploaded_by=uploaded_by.id if uploaded_by else None,
            )
            s.add(row)
            s.flush()
            audit.append(
                s,
                uploaded_by,
                "document.received",
                "document",
                row.id,
                {"filename": doc.filename, "sha256": doc.sha256, "source": source},
            )
            document_id = row.id

        # The model call happens outside any transaction.
        inv: ExtractedInvoice | None = None
        error: str | None = None
        model = "unknown"
        try:
            raw = self.extractor.extract(doc, message_text)
            model = raw.model
            inv = parse(raw)
        except ExtractionError as e:
            error = str(e)
        except LookupError as e:  # replay mode with no stored answer
            error = f"no extraction available: {e}"

        with self.sessions.begin() as s:
            agent = self.agent(s)
            invoice = InvoiceRow(
                document_id=document_id,
                extracted=inv.model_dump(mode="json") if inv else None,
                extraction_error=error,
                model=model,
                vendor_tax_id=tax_id.normalize(inv.vendor_tax_id or "") or None if inv else None,
                invoice_number=inv.invoice_number if inv else None,
                total=inv.total if inv else None,
                issue_date=inv.issue_date if inv else None,
            )
            s.add(invoice)
            s.flush()
            audit.append(
                s,
                agent,
                "invoice.extracted" if inv else "invoice.extraction_failed",
                "invoice",
                invoice.id,
                {"model": model, "error": error},
            )
            draft = self._prepare(s, agent, invoice, inv, message_text)
            return Received(draft.id, duplicate_upload=False)

    def _prepare(
        self, s: Session, agent: User, invoice: InvoiceRow, inv: ExtractedInvoice | None, message: str | None
    ) -> DraftRow:
        if inv is None:
            draft = DraftRow(
                invoice_id=invoice.id,
                state=DraftState.EXTRACTION_FAILED,
                amount=None,
                checks=[],
                flags=[],
                reasoning=[
                    f"The document could not be read automatically: {invoice.extraction_error}. "
                    "Enter it by hand or reject it."
                ],
                requires_bank_verification=False,
                pay_to=None,
                prepared_by=agent.id,
            )
        else:
            vendors = [vendor_from_row(v) for v in s.scalars(select(VendorRow))]
            history = [
                PastInvoice(r.vendor_tax_id or "", r.invoice_number or "", r.total, r.issue_date)
                for r in s.scalars(select(InvoiceRow).where(InvoiceRow.id != invoice.id))
                if r.total is not None and r.issue_date is not None
            ]
            checks = run_checks(inv, self.today())
            flags = assess(inv, RiskContext(vendors=vendors, history=history, message_text=message))
            wanted = tax_id.normalize(inv.vendor_tax_id or "")
            vendor = next((v for v in vendors if v.tax_id == wanted), None)
            d = build_draft(inv, checks, flags, vendor)
            draft = DraftRow(
                invoice_id=invoice.id,
                state=DraftState(d.status.value),
                amount=inv.total,
                checks=[{"name": c.name, "status": c.status.value, "detail": c.detail} for c in checks],
                flags=[
                    {"code": f.code.value, "severity": f.severity.value, "detail": f.detail} for f in flags
                ],
                reasoning=d.reasoning,
                requires_bank_verification=d.requires_bank_verification,
                pay_to=d.pay_to.model_dump() if d.pay_to else None,
                prepared_by=agent.id,
            )
        s.add(draft)
        s.flush()
        audit.append(
            s,
            agent,
            "draft.prepared",
            "draft",
            draft.id,
            {"state": draft.state.value, "flags": [f["code"] for f in draft.flags], "amount": draft.amount},
        )
        return draft

    # ---- decisions ---------------------------------------------------------------------------

    def approve(
        self, draft_id: int, user: User, comment: str | None, bank_verification_note: str | None
    ) -> int:
        """Approves a draft and records a simulated payment. Returns the payment id."""
        if user.role is not Role.APPROVER:
            raise Forbidden("only an approver can approve payments")
        with self.sessions.begin() as s:
            draft, document = self._load(s, draft_id)
            if draft.prepared_by == user.id:
                raise Forbidden("the preparer of a draft cannot approve it")
            if document.uploaded_by == user.id:
                raise Forbidden("you uploaded this document; another approver must decide it")
            if draft.state not in (DraftState.READY, DraftState.NEEDS_ATTENTION):
                raise Conflict(f"a {draft.state.value} draft cannot be approved")
            note = (bank_verification_note or "").strip()
            if draft.requires_bank_verification and len(note) < MIN_VERIFICATION_NOTE:
                raise Conflict(
                    "the bank details changed: confirm them by phone on a number already on file, "
                    "and record who you spoke to"
                )
            if draft.pay_to is None or draft.amount is None:
                raise Conflict("no verified vendor bank account or amount to pay")
            draft.state = DraftState.APPROVED
            draft.decided_by = user.id
            draft.decided_at = datetime.now(UTC)
            draft.decision_comment = comment
            draft.bank_verification_note = note or None
            payment = SimulatedPayment(
                draft_id=draft.id,
                amount=draft.amount,
                pay_to=draft.pay_to,
                reference="SIM-" + secrets.token_hex(5).upper(),
            )
            s.add(payment)
            s.flush()
            audit.append(
                s,
                user,
                "draft.approved",
                "draft",
                draft.id,
                {"comment": comment, "bank_verification_note": note or None},
            )
            audit.append(
                s,
                user,
                "payment.simulated",
                "payment",
                payment.id,
                {"amount": draft.amount, "pay_to": draft.pay_to, "reference": payment.reference},
            )
            return payment.id

    def reject(self, draft_id: int, user: User, comment: str) -> None:
        if user.role not in (Role.APPROVER, Role.ADMIN):
            raise Forbidden("only an approver or admin can reject")
        if not comment.strip():
            raise Conflict("a rejection needs a reason")
        with self.sessions.begin() as s:
            draft, _ = self._load(s, draft_id)
            if draft.state in (DraftState.APPROVED, DraftState.REJECTED):
                raise Conflict(f"the draft is already {draft.state.value}")
            draft.state = DraftState.REJECTED
            draft.decided_by = user.id
            draft.decided_at = datetime.now(UTC)
            draft.decision_comment = comment.strip()
            audit.append(s, user, "draft.rejected", "draft", draft.id, {"comment": comment.strip()})

    def _load(self, s: Session, draft_id: int) -> tuple[DraftRow, DocumentRow]:
        draft = s.get(DraftRow, draft_id, with_for_update=True)
        if draft is None:
            raise NotFound(f"no draft {draft_id}")
        invoice = s.get(InvoiceRow, draft.invoice_id)
        assert invoice is not None
        document = s.get(DocumentRow, invoice.document_id)
        assert document is not None
        return draft, document


def draft_summary(s: Session, draft: DraftRow) -> dict[str, Any]:
    invoice = s.get(InvoiceRow, draft.invoice_id)
    assert invoice is not None
    extracted = invoice.extracted or {}
    return {
        "id": draft.id,
        "state": draft.state.value,
        "vendor": extracted.get("vendor_name"),
        "invoice_number": invoice.invoice_number,
        "amount": None if draft.amount is None else str(draft.amount),
        "flags": [f["code"] for f in draft.flags],
        "failed_checks": [c["name"] for c in draft.checks if c["status"] == "FAIL"],
        "requires_bank_verification": draft.requires_bank_verification,
        "created_at": draft.created_at.isoformat(),
    }
