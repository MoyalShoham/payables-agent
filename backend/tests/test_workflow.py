"""End-to-end workflow against a real PostgreSQL (Testcontainers): upload, draft, approve, audit."""

from __future__ import annotations

import io
import json
import os
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from email.message import EmailMessage
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from reportlab.pdfgen import canvas
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from testcontainers.postgres import PostgresContainer

from payables import audit
from payables.api import CLIENT_HEADER, create_app
from payables.config import Settings
from payables.extraction.documents import SourceDocument
from payables.extraction.model import RawExtraction
from payables.ingest import process_inbox
from payables.schemas import BankDetails, ExtractedInvoice
from payables.services import Payables

from .conftest import ACME_BANK, ACME_ID

pytestmark = pytest.mark.db

Env = tuple[TestClient, Payables, "FakeExtractor"]


@pytest.fixture(scope="module")
def database_url() -> Iterator[str]:
    with PostgresContainer("postgres:16-alpine", driver="psycopg") as pg:
        url = pg.get_connection_url()
        os.environ["PAYABLES_DATABASE_URL"] = url
        cfg = Config(str(Path(__file__).parents[1] / "alembic.ini"))
        cfg.set_main_option("script_location", str(Path(__file__).parents[1] / "alembic"))
        command.upgrade(cfg, "head")
        yield url


class FakeExtractor:
    """Answers with whatever invoice was registered for the document's text marker."""

    def __init__(self) -> None:
        self.answers: dict[str, str] = {}

    def register(self, marker: str, inv: ExtractedInvoice | None) -> bytes:
        self.answers[marker] = "not json" if inv is None else json.dumps(inv.model_dump(mode="json"))
        buf = io.BytesIO()
        c = canvas.Canvas(buf, invariant=1, pageCompression=0)
        c.drawString(72, 720, marker)
        c.save()
        return buf.getvalue()

    def extract(self, doc: SourceDocument, message_text: str | None = None) -> RawExtraction:
        for marker, answer in self.answers.items():
            if marker.encode() in doc.content:
                return RawExtraction(text=answer, model="fake", stop_reason="end_turn")
        raise LookupError("unregistered document")


@pytest.fixture
def env(
    database_url: str, clean_invoice: ExtractedInvoice
) -> Iterator[tuple[TestClient, Payables, FakeExtractor]]:
    engine = create_engine(database_url)
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE audit_log DISABLE TRIGGER audit_log_no_truncate"))
        conn.execute(
            text(
                "TRUNCATE simulated_payments, payment_drafts, invoices, documents, vendors, "
                "audit_log, users RESTART IDENTITY CASCADE"
            )
        )
        conn.execute(text("ALTER TABLE audit_log ENABLE TRIGGER audit_log_no_truncate"))
    fake = FakeExtractor()
    service = Payables(sessionmaker(engine, expire_on_commit=False), fake, today=date(2025, 6, 15))
    settings = Settings(
        database_url=database_url,
        session_secret="test-secret",
        approver_password="approve-pw",
        admin_password="admin-pw",
    )
    service.ensure_users(settings.approver_password, settings.admin_password)
    client = TestClient(create_app(service, settings))
    admin = login(client, "admin", "admin-pw")
    r = client.post(
        "/api/vendors",
        json={"tax_id": ACME_ID, "name": clean_invoice.vendor_name, "bank": ACME_BANK.model_dump()},
        headers=admin,
    )
    assert r.status_code == 201
    client.cookies.clear()
    yield client, service, fake
    engine.dispose()


def login(client: TestClient, username: str, password: str) -> dict[str, str]:
    r = client.post(
        "/api/session", json={"username": username, "password": password}, headers={CLIENT_HEADER: "t"}
    )
    assert r.status_code == 200, r.text
    return {CLIENT_HEADER: "t"}


def upload(
    client: TestClient, content: bytes, headers: dict[str, str], message: str | None = None
) -> dict[str, object]:
    data = {"message_text": message} if message else None
    r = client.post(
        "/api/documents", files={"file": ("inv.pdf", content, "application/pdf")}, data=data, headers=headers
    )
    assert r.status_code == 201, r.text
    body: dict[str, object] = r.json()
    return body


def test_clean_invoice_is_approved_by_a_person_and_paid_in_simulation(
    env: Env, clean_invoice: ExtractedInvoice
) -> None:
    client, _service, fake = env
    admin = login(client, "admin", "admin-pw")
    draft_id = upload(client, fake.register("doc-clean", clean_invoice), admin)["draft_id"]
    client.cookies.clear()

    h = login(client, "approver", "approve-pw")
    d = client.get(f"/api/drafts/{draft_id}").json()
    assert d["state"] == "READY"
    assert d["prepared_by"] == "Payables agent"
    assert d["pay_to"]["account"] == ACME_BANK.account
    r = client.post(f"/api/drafts/{draft_id}/approve", json={"comment": "ok"}, headers=h)
    assert r.status_code == 200, r.text
    payments = client.get("/api/payments").json()
    assert payments[0]["amount"] == "1180.00"
    assert payments[0]["simulated"] is True
    assert client.get("/api/audit/verify").json() == {"intact": True, "first_broken_entry": None}
    actions = [e["action"] for e in client.get("/api/audit").json()]
    assert actions[:2] == ["payment.simulated", "draft.approved"]
    assert "draft.prepared" in actions
    # Approving twice is not possible.
    assert client.post(f"/api/drafts/{draft_id}/approve", json={}, headers=h).status_code == 409


def test_separation_of_duties(env: Env, clean_invoice: ExtractedInvoice, database_url: str) -> None:
    client, _service, fake = env
    # The agent has no password: it cannot sign in.
    r = client.post("/api/session", json={"username": "agent", "password": ""}, headers={CLIENT_HEADER: "t"})
    assert r.status_code == 401
    # An approver cannot approve a document they uploaded themselves.
    h = login(client, "approver", "approve-pw")
    draft_id = upload(client, fake.register("doc-own", clean_invoice), h)["draft_id"]
    r = client.post(f"/api/drafts/{draft_id}/approve", json={}, headers=h)
    assert r.status_code == 403
    assert "another approver" in r.json()["detail"]
    # An admin is not an approver.
    client.cookies.clear()
    h = login(client, "admin", "admin-pw")
    assert client.post(f"/api/drafts/{draft_id}/approve", json={}, headers=h).status_code == 403
    # The database refuses a draft decided by its own preparer, whatever the application does.
    engine = create_engine(database_url)
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(text("UPDATE payment_drafts SET decided_by = prepared_by, state = 'APPROVED'"))
    engine.dispose()


def test_changed_bank_details_need_a_recorded_callback(env: Env, clean_invoice: ExtractedInvoice) -> None:
    client, _service, fake = env
    moved = clean_invoice.model_copy(
        update={"bank": BankDetails(bank_code="10", branch="800", account="99887766")}
    )
    admin = login(client, "admin", "admin-pw")
    draft_id = upload(client, fake.register("doc-moved", moved), admin)["draft_id"]
    client.cookies.clear()
    h = login(client, "approver", "approve-pw")
    d = client.get(f"/api/drafts/{draft_id}").json()
    assert d["state"] == "NEEDS_ATTENTION"
    assert d["requires_bank_verification"] is True
    assert "BANK_DETAILS_CHANGED" in d["flags"]
    # Money still goes to the account on file, not the one on the invoice.
    assert d["pay_to"]["account"] == ACME_BANK.account

    r = client.post(f"/api/drafts/{draft_id}/approve", json={"bank_verification_note": "ok"}, headers=h)
    assert r.status_code == 409
    note = "Called Acme on 03-5551234 (number on file), spoke to Rina in accounts: no change."
    r = client.post(f"/api/drafts/{draft_id}/approve", json={"bank_verification_note": note}, headers=h)
    assert r.status_code == 200


def test_duplicates_and_extraction_failures_cannot_be_approved(
    env: Env, clean_invoice: ExtractedInvoice
) -> None:
    client, _service, fake = env
    admin = login(client, "admin", "admin-pw")
    first = fake.register("doc-first", clean_invoice)
    a = upload(client, first, admin)
    again = upload(client, first, admin)
    assert again == {"draft_id": a["draft_id"], "duplicate_upload": True}
    # Same vendor and invoice number, different file: blocked as a duplicate.
    b = upload(client, fake.register("doc-resent", clean_invoice), admin)["draft_id"]
    broken = upload(client, fake.register("doc-garbled", None), admin)["draft_id"]
    client.cookies.clear()
    h = login(client, "approver", "approve-pw")
    assert client.get(f"/api/drafts/{b}").json()["state"] == "BLOCKED"
    assert client.post(f"/api/drafts/{b}/approve", json={}, headers=h).status_code == 409
    assert client.get(f"/api/drafts/{broken}").json()["state"] == "EXTRACTION_FAILED"
    assert client.post(f"/api/drafts/{broken}/approve", json={}, headers=h).status_code == 409
    assert (
        client.post(f"/api/drafts/{broken}/reject", json={"comment": "unreadable"}, headers=h).status_code
        == 200
    )


def test_api_guards(env: Env, clean_invoice: ExtractedInvoice) -> None:
    client, _service, fake = env
    assert client.get("/api/drafts").status_code == 401
    r = client.post("/api/session", json={"username": "approver", "password": "x"})
    assert r.status_code == 403  # no CSRF header
    for _ in range(5):
        client.post(
            "/api/session", json={"username": "approver", "password": "x"}, headers={CLIENT_HEADER: "t"}
        )
    r = client.post(
        "/api/session", json={"username": "approver", "password": "approve-pw"}, headers={CLIENT_HEADER: "t"}
    )
    assert r.status_code == 429
    h = login(client, "admin", "admin-pw")
    r = client.post(
        "/api/documents", files={"file": ("x.pdf", b"MZ not a pdf", "application/pdf")}, headers=h
    )
    assert r.status_code == 422
    draft_id = upload(client, fake.register("doc-headers", clean_invoice), h)["draft_id"]
    doc_id = client.get(f"/api/drafts/{draft_id}").json()["document"]["id"]
    r = client.get(f"/api/documents/{doc_id}/content")
    assert r.headers["content-security-policy"].startswith("sandbox")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert client.post("/api/vendors", json={"tax_id": "123", "name": "x"}, headers=h).status_code == 422


def test_audit_log_is_append_only_and_tamper_evident(
    env: Env, clean_invoice: ExtractedInvoice, database_url: str
) -> None:
    client, _service, fake = env
    h = login(client, "admin", "admin-pw")
    upload(client, fake.register("doc-audit", clean_invoice), h)
    engine = create_engine(database_url)
    with pytest.raises(DBAPIError, match="append-only"), engine.begin() as conn:
        conn.execute(text("UPDATE audit_log SET actor = 'mallory'"))
    with pytest.raises(DBAPIError, match="append-only"), engine.begin() as conn:
        conn.execute(text("DELETE FROM audit_log"))
    # Even someone who can disable the trigger cannot rewrite history unnoticed.
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE audit_log DISABLE TRIGGER audit_log_no_change"))
        conn.execute(text('UPDATE audit_log SET details = \'{"amount": "1.00"}\' WHERE id = 2'))
        conn.execute(text("ALTER TABLE audit_log ENABLE TRIGGER audit_log_no_change"))
    with Session(engine) as s:
        assert audit.verify(s) == 2
    engine.dispose()


def test_mailbox_email_with_bank_change_text_is_flagged(
    env: Env, clean_invoice: ExtractedInvoice, tmp_path: Path
) -> None:
    client, service, fake = env
    msg = EmailMessage()
    msg["From"] = "billing@acme.example"
    msg["Subject"] = "Invoice"
    msg.set_content("Hi, please note our bank details have changed. Pay the new account from now on.")
    pdf = fake.register("doc-email", clean_invoice)
    msg.add_attachment(pdf, maintype="application", subtype="pdf", filename="invoice.pdf")
    (tmp_path / "mail.eml").write_bytes(msg.as_bytes())
    (tmp_path / "notes.txt").write_text("not an invoice")

    assert process_inbox(service, tmp_path) == 1
    assert (tmp_path / "processed" / "mail.eml").exists()
    assert (tmp_path / "rejected" / "notes.txt.reason.txt").exists()
    h = login(client, "approver", "approve-pw")
    d = client.get("/api/drafts").json()[0]
    assert "BANK_CHANGE_REQUESTED" in d["flags"]
    assert d["requires_bank_verification"] is True
    assert h


def test_amounts_are_decimal_strings(env: Env, clean_invoice: ExtractedInvoice) -> None:
    client, _service, fake = env
    h = login(client, "admin", "admin-pw")
    draft_id = upload(client, fake.register("doc-money", clean_invoice), h)["draft_id"]
    d = client.get(f"/api/drafts/{draft_id}").json()
    assert d["amount"] == "1180.00"
    assert Decimal(d["extracted"]["total"]) == Decimal("1180.00")
