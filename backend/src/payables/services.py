"""The workflow: receive a document, extract, check, draft; then a person approves or rejects.

The agent (a user with the AGENT role and no password) prepares every draft. Only an APPROVER can
decide, never the agent, and never the person who submitted the document by any channel. The
database enforces the separation too (roles, triggers, CHECK constraints; see the migrations).
Every step is written to the audit log in the same transaction as the change.

Transactions are short and never span a model call: the document is stored first, the model is
called with no transaction open, and the invoice and draft are written together afterwards. If that
second step never happens (a crash), uploading the same file again finishes the job.
"""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, load_only, sessionmaker

from payables import audit, tax_id
from payables.db import (
    DocumentRow,
    DraftRow,
    DraftState,
    InvoiceRow,
    Role,
    SessionRow,
    SimulatedPayment,
    User,
    VendorRow,
)
from payables.drafting import build_draft
from payables.extraction import documents
from payables.extraction.claude import Extractor
from payables.extraction.model import ExtractionError, parse
from payables.extraction.recording import MissingRecordingError
from payables.risk import PastInvoice, RiskContext, RiskFlag, Vendor, assess
from payables.schemas import BankDetails, ExtractedInvoice, validate_master_bank
from payables.validation import Check, run_checks

log = logging.getLogger(__name__)

AGENT_USERNAME = "agent"
CALLBACK_MAX_AGE = timedelta(days=7)
CLOCK_SKEW = timedelta(minutes=5)
MESSAGE_LIMIT = 4000
_hasher = PasswordHasher()
#: Verified against when the username does not exist, so a miss costs as much as a wrong password.
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(16))
#: The agent's database role may not read password hashes.
_USER_FIELDS = load_only(User.id, User.username, User.display_name, User.role, User.email)


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


@dataclass(frozen=True)
class Callback:
    """The phone call that confirms a vendor's bank account before a bank-change draft is paid."""

    phone: str
    person: str
    at: datetime


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def _verify(hashed: str, password: str) -> bool:
    try:
        return _hasher.verify(hashed, password)
    except (VerifyMismatchError, InvalidHashError):
        return False


def normalize_phone(value: str) -> str:
    """Digits only, with +972 written as a leading 0."""
    digits = re.sub(r"\D", "", value or "")
    if digits.startswith("972"):
        digits = "0" + digits[3:]
    return digits


def validate_phones(phones: list[str]) -> list[str]:
    out = []
    for p in phones:
        digits = normalize_phone(p)
        if not (digits.startswith("0") and 9 <= len(digits) <= 10):
            raise ValueError(f"{p!r} is not an Israeli phone number")
        out.append(p.strip())
    return out


def vendor_from_row(v: VendorRow) -> Vendor:
    bank = None
    if v.iban or v.account:
        bank = BankDetails(
            bank_name=v.bank_name, bank_code=v.bank_code, branch=v.branch, account=v.account, iban=v.iban
        )
    return Vendor(tax_id=v.tax_id, name=v.name, bank=bank)


class Payables:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        extractor: Extractor,
        audit_key: bytes,
        today: date | None = None,
        anchors: Path | None = None,
    ) -> None:
        self.sessions = sessions
        self.extractor = extractor
        self.audit_key = audit_key
        self.fixed_today = today
        self.anchors = anchors

    def today(self) -> date:
        return self.fixed_today or datetime.now(UTC).date()

    def _audit(
        self, s: Session, actor: User | None, action: str, entity: str, entity_id: int | None, **details: Any
    ) -> None:
        audit.append(s, self.audit_key, actor, action, entity, entity_id, details)

    def checkpoint(self) -> None:
        """Appends the audit head to the anchors file, if one is configured."""
        if self.anchors is None:
            return
        with self.sessions.begin() as s:
            audit.checkpoint(s, self.anchors)

    # ---- users, sessions and vendors ---------------------------------------------------------

    def ensure_users(
        self, approver_password: str, admin_password: str, emails: dict[str, str] | None = None
    ) -> None:
        """Creates the demo users if missing. Safe when several processes start at once."""
        emails = emails or {}
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
                        email=emails.get(username),
                        password_hash=hash_password(password) if password else None,
                    )
                    .on_conflict_do_nothing(index_elements=["username"])
                )

    def agent(self, s: Session) -> User:
        return s.scalars(select(User).options(_USER_FIELDS).where(User.username == AGENT_USERNAME)).one()

    def authenticate(self, username: str, password: str) -> User | None:
        with self.sessions() as s:
            user = s.scalars(select(User).where(User.username == username)).first()
            hashed = user.password_hash if user is not None else None
            if user is None or not hashed:
                _verify(_DUMMY_HASH, password)  # the same work as a real check: no timing oracle
                return None
            if not _verify(hashed, password):
                return None
            s.expunge(user)
            return user

    def open_session(self, user: User, hours: int) -> str:
        token = secrets.token_urlsafe(32)
        with self.sessions.begin() as s:
            s.add(
                SessionRow(
                    token_hash=_token_hash(token),
                    user_id=user.id,
                    expires_at=datetime.now(UTC) + timedelta(hours=hours),
                )
            )
            self._audit(s, user, "session.opened", "user", user.id)
        return token

    def session_user(self, token: str) -> User | None:
        """The signed-in user for a session token, if the session is neither expired nor revoked."""
        with self.sessions() as s:
            user = s.scalars(
                select(User)
                .join(SessionRow, SessionRow.user_id == User.id)
                .where(
                    SessionRow.token_hash == _token_hash(token),
                    SessionRow.revoked_at.is_(None),
                    SessionRow.expires_at > datetime.now(UTC),
                )
            ).first()
            if user is None or not user.password_hash:
                return None
            s.expunge(user)
            return user

    def close_session(self, token: str, user: User) -> None:
        with self.sessions.begin() as s:
            s.execute(
                update(SessionRow)
                .where(SessionRow.token_hash == _token_hash(token), SessionRow.revoked_at.is_(None))
                .values(revoked_at=datetime.now(UTC))
            )
            self._audit(s, user, "session.closed", "user", user.id)

    def upsert_vendor(
        self, actor: User, tax: str, name: str, bank: BankDetails | None, phones: list[str] | None = None
    ) -> int:
        """Admin only. Raises ValueError (shown to the user) for an invalid tax ID, account or phone."""
        if actor.role is not Role.ADMIN:
            raise Forbidden("only an admin can change the vendor master")
        normalized = tax_id.normalize(tax)
        if not tax_id.is_valid(normalized):
            raise ValueError("invalid Israeli tax ID")
        checked = validate_master_bank(bank) if bank is not None else None
        numbers = validate_phones(phones) if phones is not None else None
        with self.sessions.begin() as s:
            row = s.scalars(select(VendorRow).where(VendorRow.tax_id == normalized)).first()
            before = vendor_from_row(row).bank if row else None
            phones_before = list(row.phones) if row else []
            if row is None:
                row = VendorRow(tax_id=normalized, name=name, phones=[])
                s.add(row)
            row.name = name
            if checked is not None:
                row.bank_name, row.bank_code, row.branch = (
                    checked.bank_name,
                    checked.bank_code,
                    checked.branch,
                )
                row.account, row.iban = checked.account, checked.iban
            if numbers is not None:
                row.phones = numbers
            s.flush()
            self._audit(
                s,
                actor,
                "vendor.saved",
                "vendor",
                row.id,
                tax_id=normalized,
                name=name,
                bank_before=before.model_dump() if before else None,
                bank_after=checked.model_dump() if checked else None,
                phones_before=phones_before,
                phones_after=list(row.phones),
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
        submitted_by_email: str | None = None,
    ) -> Received:
        """Stores the document, extracts it, runs the checks and prepares a draft. Idempotent per file.

        Raises RejectedDocumentError for a file that is not an acceptable document, and Conflict for a
        document already on file as paid history."""
        doc = documents.load(content, filename)
        message = (message_text or "").strip()[:MESSAGE_LIMIT] or None
        email = (submitted_by_email or "").strip().lower()[:200] or None
        with self.sessions.begin() as s:
            submitter = uploaded_by
            if submitter is None and email:
                submitter = s.scalars(select(User).options(_USER_FIELDS).where(User.email == email)).first()
            document_id = s.execute(
                pg_insert(DocumentRow)
                .values(
                    sha256=doc.sha256,
                    filename=doc.filename,
                    media_type=doc.original_media_type or doc.media_type,
                    content=doc.original or doc.content,
                    original_sha256=doc.original_sha256,
                    normalized=doc.content if doc.original and doc.original != doc.content else None,
                    preview_png=doc.preview_png,
                    source=source,
                    message_text=message,
                    uploaded_by=submitter.id if submitter else None,
                    submitted_by_email=email,
                )
                .on_conflict_do_nothing(index_elements=["sha256"])
                .returning(DocumentRow.id)
            ).scalar()
            if document_id is None:
                existing = s.scalars(
                    select(DocumentRow).where(DocumentRow.sha256 == doc.sha256).with_for_update()
                ).one()
                done = self._resubmitted(s, existing, doc.filename, source, submitter, message, email)
                if done is not None:
                    return done
                document_id, message = existing.id, existing.message_text
            else:
                self._audit(
                    s,
                    submitter,
                    "document.received",
                    "document",
                    document_id,
                    filename=doc.filename,
                    sha256=doc.sha256,
                    original_sha256=doc.original_sha256,
                    source=source,
                    submitted_by_email=email,
                )
        return self._extract_and_draft(document_id, doc, message)

    def _resubmitted(
        self,
        s: Session,
        existing: DocumentRow,
        filename: str,
        source: str,
        submitter: User | None,
        message: str | None,
        email: str | None,
    ) -> Received | None:
        """A file already on record. Returns its draft, or None if it still needs extracting (an
        earlier attempt stopped between storing the document and writing its draft)."""
        invoice = s.scalars(select(InvoiceRow).where(InvoiceRow.document_id == existing.id)).first()
        if invoice is None:
            return None
        draft = s.scalars(select(DraftRow).where(DraftRow.invoice_id == invoice.id)).first()
        if draft is None:
            raise Conflict("this document is already on file as a paid invoice from the payment history")
        new_message = message is not None and message not in (existing.message_text or "")
        self._audit(
            s,
            submitter,
            "document.duplicate_upload",
            "document",
            existing.id,
            filename=filename,
            source=source,
            submitted_by_email=email,
            message=message,
        )
        if new_message and message is not None:
            stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
            existing.message_text = "\n\n".join(
                p for p in (existing.message_text, f"[message received again, {stamp}]\n{message}") if p
            )[: MESSAGE_LIMIT * 4]
            if draft.state not in (DraftState.APPROVED, DraftState.REJECTED) and invoice.extracted:
                agent = self.agent(s)
                inv = ExtractedInvoice.model_validate(invoice.extracted)
                self._apply(s, draft, invoice, inv, existing.message_text)
                self._audit(
                    s,
                    agent,
                    "draft.reassessed",
                    "draft",
                    draft.id,
                    reason="a new covering message arrived",
                    state=draft.state.value,
                    flags=[f["code"] for f in draft.flags],
                )
        return Received(draft.id, duplicate_upload=True)

    def _extract_and_draft(
        self, document_id: int, doc: documents.SourceDocument, message: str | None
    ) -> Received:
        # The model call happens outside any transaction. Whatever goes wrong becomes a draft that
        # says so; details go to the server log, not to the reviewer.
        inv: ExtractedInvoice | None = None
        error: str | None = None
        model = "unknown"
        try:
            raw = self.extractor.extract(doc, message)
            model = raw.model[:80]
            inv = parse(raw)
        except ExtractionError as e:
            error = str(e)
        except MissingRecordingError:
            error = "no recorded model answer is available for this document (offline replay mode)"
        except Exception:
            log.exception("extraction failed for document %s", document_id)
            error = "the model could not be reached or failed; upload the file again to retry"

        with self.sessions.begin() as s:
            # Serialise with a concurrent upload of the same file: whoever comes second reuses the draft.
            s.execute(select(DocumentRow.id).where(DocumentRow.id == document_id).with_for_update())
            invoice = s.scalars(select(InvoiceRow).where(InvoiceRow.document_id == document_id)).first()
            if invoice is not None:
                draft = s.scalars(select(DraftRow).where(DraftRow.invoice_id == invoice.id)).one()
                return Received(draft.id, duplicate_upload=True)
            agent = self.agent(s)
            invoice = InvoiceRow(
                document_id=document_id,
                extracted=inv.model_dump(mode="json") if inv else None,
                extraction_error=error,
                model=model,
                vendor_tax_id=(tax_id.normalize(inv.vendor_tax_id or "") or None) if inv else None,
                invoice_number=(inv.invoice_number or "")[:60] or None if inv else None,
                total=inv.total if inv else None,
                currency=inv.currency if inv else "ILS",
                issue_date=inv.issue_date if inv else None,
            )
            s.add(invoice)
            s.flush()
            self._audit(
                s,
                agent,
                "invoice.extracted" if inv else "invoice.extraction_failed",
                "invoice",
                invoice.id,
                model=model,
                error=error,
            )
            draft = DraftRow(
                invoice_id=invoice.id,
                state=DraftState.EXTRACTION_FAILED,
                amount=None,
                checks=[],
                flags=[],
                reasoning=[
                    f"The document could not be read automatically: {error}. Reject it, or ask the "
                    "vendor for a clearer copy and upload that."
                ],
                requires_bank_verification=False,
                pay_to=None,
                prepared_by=agent.id,
            )
            if inv is not None:
                self._apply(s, draft, invoice, inv, message)
            s.add(draft)
            s.flush()
            self._audit(
                s,
                agent,
                "draft.prepared",
                "draft",
                draft.id,
                state=draft.state.value,
                flags=[f["code"] for f in draft.flags],
                amount=draft.amount,
                currency=draft.currency,
            )
            return Received(draft.id, duplicate_upload=False)

    def _assess(
        self, s: Session, inv: ExtractedInvoice, message: str | None, exclude_invoice: int | None
    ) -> tuple[list[Check], list[RiskFlag], Vendor | None]:
        vendors = [vendor_from_row(v) for v in s.scalars(select(VendorRow))]
        rows = s.execute(
            select(
                InvoiceRow.vendor_tax_id,
                InvoiceRow.invoice_number,
                InvoiceRow.total,
                InvoiceRow.issue_date,
                DraftRow.state,
            )
            .outerjoin(DraftRow, DraftRow.invoice_id == InvoiceRow.id)
            .where(InvoiceRow.id != (exclude_invoice or 0))
        )
        history = [
            PastInvoice(
                r.vendor_tax_id or "",
                r.invoice_number or "",
                r.total,
                r.issue_date,
                rejected=r.state == DraftState.REJECTED,
            )
            for r in rows
            if r.total is not None and r.issue_date is not None
        ]
        checks = run_checks(inv, self.today())
        flags = assess(inv, RiskContext(vendors=vendors, history=history, message_text=message))
        wanted = tax_id.normalize(inv.vendor_tax_id or "")
        vendor = next((v for v in vendors if wanted and v.tax_id == wanted), None)
        return checks, flags, vendor

    def _apply(
        self, s: Session, draft: DraftRow, invoice: InvoiceRow, inv: ExtractedInvoice, message: str | None
    ) -> None:
        """(Re)computes a draft's checks, flags, reasoning and payee from the extraction."""
        checks, flags, vendor = self._assess(s, inv, message, invoice.id)
        d = build_draft(inv, checks, flags, vendor)
        draft.state = DraftState(d.status.value)
        # A non-positive total is blocked by the checks; the draft carries no amount to pay.
        draft.amount = inv.total if inv.total is not None and inv.total > 0 else None
        draft.currency = inv.currency
        draft.checks = [{"name": c.name, "status": c.status.value, "detail": c.detail} for c in checks]
        draft.flags = [
            {"code": f.code.value, "severity": f.severity.value, "detail": f.detail} for f in flags
        ]
        draft.reasoning = d.reasoning
        draft.requires_bank_verification = d.requires_bank_verification
        draft.pay_to = d.pay_to.model_dump() if d.pay_to else None

    # ---- decisions ---------------------------------------------------------------------------

    def recheck(self, draft_id: int, user: User) -> None:
        """Re-runs the checks against the current vendor master and history (after an admin fixed the
        master, for example)."""
        if user.role not in (Role.APPROVER, Role.ADMIN):
            raise Forbidden("only an approver or admin can re-check a draft")
        with self.sessions.begin() as s:
            draft, invoice, document = self._load(s, draft_id)
            if draft.state in (DraftState.APPROVED, DraftState.REJECTED):
                raise Conflict(f"the draft is already {draft.state.value}")
            if not invoice.extracted:
                raise Conflict("nothing was extracted from this document, so there is nothing to re-check")
            before = (draft.state.value, draft.pay_to)
            self._apply(
                s, draft, invoice, ExtractedInvoice.model_validate(invoice.extracted), document.message_text
            )
            self._audit(
                s,
                user,
                "draft.rechecked",
                "draft",
                draft.id,
                state_before=before[0],
                state=draft.state.value,
                pay_to_before=before[1],
                pay_to=draft.pay_to,
                flags=[f["code"] for f in draft.flags],
            )

    def approve(self, draft_id: int, user: User, comment: str | None, callback: Callback | None) -> int:
        """Approves a draft and records a simulated payment. Returns the payment id."""
        if user.role is not Role.APPROVER:
            raise Forbidden("only an approver can approve payments")
        with self.sessions.begin() as s:
            draft, invoice, document = self._load(s, draft_id)
            if draft.prepared_by == user.id:
                raise Forbidden("the preparer of a draft cannot approve it")
            if document.uploaded_by == user.id:
                raise Forbidden("you submitted this document; another approver must decide it")
            if draft.state not in (DraftState.READY, DraftState.NEEDS_ATTENTION):
                raise Conflict(f"a {draft.state.value} draft cannot be approved")
            if draft.pay_to is None or draft.amount is None:
                raise Conflict("no verified vendor bank account or amount to pay")
            vendor = s.scalars(
                select(VendorRow).where(VendorRow.tax_id == (invoice.vendor_tax_id or ""))
            ).first()
            current = vendor_from_row(vendor).bank if vendor else None
            if current is None or current.model_dump() != draft.pay_to:
                raise Conflict(
                    "the vendor's bank account on file changed after this draft was prepared: "
                    "re-check the draft, then decide"
                )
            call = None
            if draft.requires_bank_verification:
                assert vendor is not None
                call = self._check_callback(callback, vendor)
            draft.state = DraftState.APPROVED
            draft.decided_by = user.id
            draft.decided_at = datetime.now(UTC)
            draft.decision_comment = comment
            if call is not None:
                draft.callback_phone, draft.callback_person, draft.callback_at = (
                    call.phone,
                    call.person,
                    call.at,
                )
            s.flush()
            payment = SimulatedPayment(
                draft_id=draft.id,
                amount=draft.amount,
                currency=draft.currency,
                pay_to=draft.pay_to,
                reference="SIM-" + secrets.token_hex(5).upper(),
            )
            s.add(payment)
            s.flush()
            self._audit(
                s,
                user,
                "draft.approved",
                "draft",
                draft.id,
                comment=comment,
                callback=None
                if call is None
                else {"phone": call.phone, "person": call.person, "at": call.at},
            )
            self._audit(
                s,
                user,
                "payment.simulated",
                "payment",
                payment.id,
                amount=draft.amount,
                currency=draft.currency,
                pay_to=draft.pay_to,
                reference=payment.reference,
            )
            payment_id = payment.id
        self.checkpoint()
        return payment_id

    def _check_callback(self, callback: Callback | None, vendor: VendorRow) -> Callback:
        if callback is None:
            raise Conflict(
                "the bank details changed or cannot be confirmed: call the vendor on a number from the "
                "vendor master and record the call"
            )
        on_file = {normalize_phone(p): p for p in vendor.phones}
        if not on_file:
            raise Conflict("the vendor master has no phone number for this vendor: an admin must add one")
        dialled = normalize_phone(callback.phone)
        if dialled not in on_file:
            raise Conflict("call a number from the vendor master, not one from the invoice or the email")
        person = " ".join(callback.person.split())
        if len(person) < 2:
            raise Conflict("record who confirmed the account")
        at = callback.at if callback.at.tzinfo else callback.at.replace(tzinfo=UTC)
        now = datetime.now(UTC)
        if at > now + CLOCK_SKEW:
            raise Conflict("the call cannot be in the future")
        if at < now - CALLBACK_MAX_AGE:
            raise Conflict("the call is more than 7 days old: call again")
        return Callback(on_file[dialled], person[:120], at)

    def reject(self, draft_id: int, user: User, comment: str) -> None:
        if user.role not in (Role.APPROVER, Role.ADMIN):
            raise Forbidden("only an approver or admin can reject")
        if not comment.strip():
            raise Conflict("a rejection needs a reason")
        with self.sessions.begin() as s:
            draft, _, _ = self._load(s, draft_id)
            if draft.state in (DraftState.APPROVED, DraftState.REJECTED):
                raise Conflict(f"the draft is already {draft.state.value}")
            draft.state = DraftState.REJECTED
            draft.decided_by = user.id
            draft.decided_at = datetime.now(UTC)
            draft.decision_comment = comment.strip()
            self._audit(s, user, "draft.rejected", "draft", draft.id, comment=comment.strip())
        self.checkpoint()

    def _load(self, s: Session, draft_id: int) -> tuple[DraftRow, InvoiceRow, DocumentRow]:
        # FOR UPDATE: concurrent decisions on one draft queue here and the losers see its new state.
        draft = s.get(DraftRow, draft_id, with_for_update=True)
        if draft is None:
            raise NotFound(f"no draft {draft_id}")
        invoice = s.get(InvoiceRow, draft.invoice_id)
        assert invoice is not None
        document = s.get(DocumentRow, invoice.document_id)
        assert document is not None
        return draft, invoice, document


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


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
        "currency": draft.currency,
        "flags": [f["code"] for f in draft.flags],
        "failed_checks": [c["name"] for c in draft.checks if c["status"] == "FAIL"],
        "requires_bank_verification": draft.requires_bank_verification,
        "created_at": draft.created_at.isoformat(),
    }


def draft_detail(s: Session, draft: DraftRow) -> dict[str, Any]:
    invoice = s.get(InvoiceRow, draft.invoice_id)
    assert invoice is not None
    doc = s.get(DocumentRow, invoice.document_id)
    assert doc is not None
    payment = s.scalars(select(SimulatedPayment).where(SimulatedPayment.draft_id == draft.id)).first()
    names = {u.id: u.display_name for u in s.scalars(select(User).options(_USER_FIELDS))}
    vendor = s.scalars(select(VendorRow).where(VendorRow.tax_id == (invoice.vendor_tax_id or ""))).first()
    current = vendor_from_row(vendor).bank if vendor else None
    return {
        **draft_summary(s, draft),
        "extracted": invoice.extracted,
        "extraction_error": invoice.extraction_error,
        "model": invoice.model,
        "checks": draft.checks,
        "flag_details": draft.flags,
        "reasoning": draft.reasoning,
        "pay_to": draft.pay_to,
        "vendor_phones": list(vendor.phones) if vendor else [],
        # The master changed after the draft was prepared: approval is refused until a re-check.
        "master_changed": draft.pay_to is not None
        and (current is None or current.model_dump() != draft.pay_to),
        "prepared_by": names.get(draft.prepared_by),
        "decided_by": names.get(draft.decided_by) if draft.decided_by else None,
        "decided_at": draft.decided_at.isoformat() if draft.decided_at else None,
        "decision_comment": draft.decision_comment,
        "callback": None
        if draft.callback_at is None
        else {
            "phone": draft.callback_phone,
            "person": draft.callback_person,
            "at": draft.callback_at.isoformat(),
        },
        "document": {
            "id": doc.id,
            "filename": doc.filename,
            "media_type": doc.media_type,
            "source": doc.source,
            "message_text": doc.message_text,
            "uploaded_by": names.get(doc.uploaded_by) if doc.uploaded_by else None,
            "submitted_by_email": doc.submitted_by_email,
        },
        "payment": None
        if payment is None
        else {
            "reference": payment.reference,
            "amount": str(payment.amount),
            "currency": payment.currency,
            "simulated": True,
        },
    }
