"""End to end against a real PostgreSQL (Testcontainers), with the production database roles: the API
runs as payables_api and the inbox agent as payables_agent, exactly as in Docker Compose."""

from __future__ import annotations

import io
import json
import os
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from PIL import Image
from reportlab.pdfgen import canvas
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from testcontainers.community.postgres import PostgresContainer

from payables import audit
from payables.api import CLIENT_HEADER, create_app
from payables.config import Settings
from payables.db import AGENT_ROLE, API_ROLE, DocumentRow, InvoiceRow
from payables.dbsetup import set_role_passwords
from payables.extraction import documents
from payables.extraction.documents import SourceDocument
from payables.extraction.model import RawExtraction
from payables.ingest import process_inbox
from payables.schemas import BankDetails, ExtractedInvoice
from payables.services import Callback, Conflict, Payables, Received

from .conftest import ACME_BANK, ACME_ID

pytestmark = pytest.mark.db

AUDIT_KEY = b"k" * 32
SECRET = "s" * 40
PHONE = "03-5551234"
APPROVER_EMAIL = "dana@shaked.example"


@pytest.fixture(scope="module")
def urls() -> Iterator[dict[str, str]]:
    with PostgresContainer("postgres:16-alpine", driver="psycopg") as pg:
        superuser = pg.get_connection_url()
        os.environ["PAYABLES_DATABASE_URL"] = superuser
        cfg = Config(str(Path(__file__).parents[1] / "alembic.ini"))
        cfg.set_main_option("script_location", str(Path(__file__).parents[1] / "alembic"))
        command.upgrade(cfg, "head")
        passwords = {API_ROLE: "api-" + "p" * 32, AGENT_ROLE: "agent-" + "p" * 32}
        set_role_passwords(superuser, passwords)
        base = make_url(superuser)
        yield {
            "superuser": superuser,
            **{
                role: base.set(username=role, password=pw).render_as_string(hide_password=False)
                for role, pw in passwords.items()
            },
        }


class FakeExtractor:
    """Answers with whatever invoice was registered for the document's text marker."""

    def __init__(self) -> None:
        self.answers: dict[str, str] = {}
        self.fail: Exception | None = None

    def register(self, marker: str, inv: ExtractedInvoice | None) -> bytes:
        self.answers[marker] = "not json" if inv is None else json.dumps(inv.model_dump(mode="json"))
        buf = io.BytesIO()
        c = canvas.Canvas(buf, invariant=1, pageCompression=0)
        c.drawString(72, 720, marker)
        c.save()
        return buf.getvalue()

    def extract(self, doc: SourceDocument, message_text: str | None = None) -> RawExtraction:
        if self.fail is not None:
            raise self.fail
        for marker, answer in self.answers.items():
            if marker.encode() in doc.content:
                return RawExtraction(text=answer, model="fake", stop_reason="end_turn")
        raise LookupError("unregistered document")


@dataclass
class Env:
    client: TestClient
    api: Payables
    agent: Payables
    fake: FakeExtractor
    urls: dict[str, str]
    anchors: Path

    def engine(self, role: str) -> Engine:
        return create_engine(self.urls[role])

    def receive(self, marker: str, inv: ExtractedInvoice, message: str | None = None) -> Received:
        """As the inbox agent (payables_agent role)."""
        return self.agent.receive(self.fake.register(marker, inv), f"{marker}.pdf", "INBOX", None, message)


@pytest.fixture
def env(urls: dict[str, str], clean_invoice: ExtractedInvoice, tmp_path: Path) -> Iterator[Env]:
    root = create_engine(urls["superuser"])
    with root.begin() as conn:
        conn.execute(text("ALTER TABLE audit_log DISABLE TRIGGER audit_log_no_truncate"))
        conn.execute(
            text(
                "TRUNCATE simulated_payments, payment_drafts, invoices, documents, vendors, sessions, "
                "audit_log, users RESTART IDENTITY CASCADE"
            )
        )
        conn.execute(text("ALTER TABLE audit_log ENABLE TRIGGER audit_log_no_truncate"))
    fake = FakeExtractor()
    anchors = tmp_path / "anchors.jsonl"
    Payables(sessionmaker(root), fake, AUDIT_KEY).ensure_users(
        "approve-pw", "admin-pw", {"approver": APPROVER_EMAIL}
    )
    engines = {role: create_engine(urls[role]) for role in (API_ROLE, AGENT_ROLE)}

    def service(role: str) -> Payables:
        return Payables(
            sessionmaker(engines[role], expire_on_commit=False), fake, AUDIT_KEY, date(2025, 6, 15), anchors
        )

    api = service(API_ROLE)
    client = TestClient(create_app(api, Settings(database_url=urls[API_ROLE], session_secret=SECRET)))
    admin = login(client, "admin", "admin-pw")
    r = client.post(
        "/api/vendors",
        json={
            "tax_id": ACME_ID,
            "name": clean_invoice.vendor_name,
            "bank": ACME_BANK.model_dump(),
            "phones": [PHONE],
        },
        headers=admin,
    )
    assert r.status_code == 201, r.text
    client.cookies.clear()
    yield Env(client, api, service(AGENT_ROLE), fake, urls, anchors)
    for e in (root, *engines.values()):
        e.dispose()


def login(client: TestClient, username: str, password: str, ip: str = "10.0.0.1") -> dict[str, str]:
    r = client.post(
        "/api/session",
        json={"username": username, "password": password},
        headers={CLIENT_HEADER: "t", "X-Real-IP": ip},
    )
    assert r.status_code == 200, r.text
    return {CLIENT_HEADER: "t"}


def upload(
    client: TestClient, content: bytes, headers: dict[str, str], message: str | None = None
) -> dict[str, Any]:
    data = {"message_text": message} if message else None
    r = client.post(
        "/api/documents", files={"file": ("inv.pdf", content, "application/pdf")}, data=data, headers=headers
    )
    assert r.status_code == 201, r.text
    body: dict[str, Any] = r.json()
    return body


def as_approver(env: Env) -> dict[str, str]:
    env.client.cookies.clear()
    return login(env.client, "approver", "approve-pw")


def callback(phone: str = PHONE, minutes_ago: int = 10, person: str = "Rina, accounts") -> dict[str, str]:
    at = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    return {"phone": phone, "person": person, "at": at.isoformat()}


def moved(inv: ExtractedInvoice, number: str = "INV-2041") -> ExtractedInvoice:
    return inv.model_copy(
        update={
            "bank": BankDetails(bank_code="10", branch="800", account="99887766"),
            "invoice_number": number,
        }
    )


def numbered(inv: ExtractedInvoice, number: str) -> ExtractedInvoice:
    return inv.model_copy(update={"invoice_number": number})


# ---- the happy path ----------------------------------------------------------------------------


def test_clean_invoice_is_approved_by_a_person_and_paid_in_simulation(
    env: Env, clean_invoice: ExtractedInvoice
) -> None:
    client = env.client
    admin = login(client, "admin", "admin-pw")
    draft_id = upload(client, env.fake.register("doc-clean", clean_invoice), admin)["draft_id"]
    h = as_approver(env)
    d = client.get(f"/api/drafts/{draft_id}").json()
    assert d["state"] == "READY"
    assert d["prepared_by"] == "Payables agent"
    assert d["pay_to"]["account"] == ACME_BANK.account
    assert d["currency"] == "ILS"
    r = client.post(f"/api/drafts/{draft_id}/approve", json={"comment": "ok"}, headers=h)
    assert r.status_code == 200, r.text
    payments = client.get("/api/payments").json()
    assert payments[0]["amount"] == "1180.00"
    assert payments[0]["currency"] == "ILS"
    assert payments[0]["simulated"] is True
    verified = client.get("/api/audit/verify").json()
    assert verified["intact"] is True
    assert verified["checkpoint"]["seq"] == verified["entries"]  # the approval wrote a checkpoint
    actions = [e["action"] for e in client.get("/api/audit").json()]
    assert actions[:2] == ["payment.simulated", "draft.approved"]
    assert "draft.prepared" in actions
    assert client.post(f"/api/drafts/{draft_id}/approve", json={}, headers=h).status_code == 409


# ---- separation of duties ----------------------------------------------------------------------


def test_people_cannot_approve_what_they_submitted_or_outside_their_role(
    env: Env, clean_invoice: ExtractedInvoice, tmp_path: Path
) -> None:
    client = env.client
    r = client.post("/api/session", json={"username": "agent", "password": ""}, headers={CLIENT_HEADER: "t"})
    assert r.status_code == 401  # the agent has no password
    h = login(client, "approver", "approve-pw")
    own = upload(client, env.fake.register("doc-own", clean_invoice), h)["draft_id"]
    r = client.post(f"/api/drafts/{own}/approve", json={}, headers=h)
    assert r.status_code == 403
    assert "another approver" in r.json()["detail"]

    # The same rule for an emailed invoice whose From: address is the approver's.
    msg = EmailMessage()
    msg["From"] = f"Dana <{APPROVER_EMAIL.upper()}>"
    msg.set_content("Invoice attached.")
    pdf = env.fake.register("doc-mailed", numbered(clean_invoice, "INV-7"))
    msg.add_attachment(pdf, maintype="application", subtype="pdf", filename="i.pdf")
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "mail.eml").write_bytes(msg.as_bytes())
    assert process_inbox(env.agent, inbox) == 1
    mailed = client.get("/api/drafts").json()[0]["id"]
    assert client.get(f"/api/drafts/{mailed}").json()["document"]["uploaded_by"] == "Dana (approver)"
    assert client.post(f"/api/drafts/{mailed}/approve", json={}, headers=h).status_code == 403

    client.cookies.clear()
    h = login(client, "admin", "admin-pw")
    assert client.post(f"/api/drafts/{own}/approve", json={}, headers=h).status_code == 403


def test_the_agent_role_cannot_decide_or_pay_and_no_app_role_can_disable_triggers(
    env: Env, clean_invoice: ExtractedInvoice
) -> None:
    draft_id = env.receive("doc-roles", clean_invoice).draft_id
    agent_db, api_db = env.engine(AGENT_ROLE), env.engine(API_ROLE)
    pay = text(
        "INSERT INTO simulated_payments (draft_id, amount, currency, pay_to, reference) "
        "VALUES (:id, 1180, 'ILS', '{}', 'X')"
    )
    with pytest.raises(DBAPIError, match="only payables_api records decisions"), agent_db.begin() as conn:
        conn.execute(text("UPDATE payment_drafts SET state = 'APPROVED' WHERE id = :id"), {"id": draft_id})
    with pytest.raises(DBAPIError, match="permission denied"), agent_db.begin() as conn:
        conn.execute(text("UPDATE payment_drafts SET decided_by = 2 WHERE id = :id"), {"id": draft_id})
    with pytest.raises(DBAPIError, match="permission denied"), agent_db.begin() as conn:
        conn.execute(pay, {"id": draft_id})
    with pytest.raises(DBAPIError, match="permission denied"), agent_db.begin() as conn:
        conn.execute(text("SELECT password_hash FROM users"))
    for engine in (agent_db, api_db):
        for statement in (
            "ALTER TABLE audit_log DISABLE TRIGGER audit_log_no_change",
            "ALTER TABLE payment_drafts DISABLE TRIGGER payment_drafts_guard",
            "ALTER TABLE payment_drafts DROP CONSTRAINT bank_change_needs_callback",
        ):
            with pytest.raises(DBAPIError, match="must be owner"), engine.begin() as conn:
                conn.execute(text(statement))
    # The API role records decisions, but the database still refuses a draft decided by its own
    # preparer, and a payment without an approved draft behind it.
    with pytest.raises(IntegrityError, match="preparer_cannot_decide"), api_db.begin() as conn:
        conn.execute(text("UPDATE payment_drafts SET decided_by = prepared_by, state = 'APPROVED'"))
    with pytest.raises(DBAPIError, match="needs an approved draft"), api_db.begin() as conn:
        conn.execute(pay, {"id": draft_id})
    agent_db.dispose()
    api_db.dispose()


def test_concurrent_approvals_produce_exactly_one_payment(env: Env, clean_invoice: ExtractedInvoice) -> None:
    """Twelve approvals of one draft at once: one wins, the rest see it decided. Without the row lock
    the losers would hit the decided-draft trigger instead of a clean Conflict."""
    draft_id = env.receive("doc-race", clean_invoice).draft_id
    approver = env.api.authenticate("approver", "approve-pw")
    assert approver is not None
    barrier = threading.Barrier(12)
    outcomes: list[str] = []

    def go() -> None:
        barrier.wait()
        try:
            env.api.approve(draft_id, approver, None, None)
            outcomes.append("approved")
        except Conflict:
            outcomes.append("conflict")
        except Exception as e:
            outcomes.append(type(e).__name__)

    threads = [threading.Thread(target=go) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(outcomes) == ["approved"] + ["conflict"] * 11
    with env.engine(API_ROLE).connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM simulated_payments")).scalar() == 1


def test_a_decided_draft_cannot_change(env: Env, clean_invoice: ExtractedInvoice) -> None:
    draft_id = env.receive("doc-final", clean_invoice).draft_id
    approver = env.api.authenticate("approver", "approve-pw")
    assert approver is not None
    env.api.reject(draft_id, approver, "not ours")
    with pytest.raises(DBAPIError, match="decided draft cannot change"), env.engine(API_ROLE).begin() as c:
        c.execute(text("UPDATE payment_drafts SET state = 'READY' WHERE id = :id"), {"id": draft_id})


# ---- bank changes ------------------------------------------------------------------------------


def test_changed_bank_details_need_a_structured_callback_on_a_number_on_file(
    env: Env, clean_invoice: ExtractedInvoice
) -> None:
    client = env.client
    admin = login(client, "admin", "admin-pw")
    draft_id = upload(client, env.fake.register("doc-moved", moved(clean_invoice)), admin)["draft_id"]
    h = as_approver(env)
    d = client.get(f"/api/drafts/{draft_id}").json()
    assert d["state"] == "NEEDS_ATTENTION"
    assert d["requires_bank_verification"] is True
    assert "BANK_DETAILS_CHANGED" in d["flags"]
    assert d["pay_to"]["account"] == ACME_BANK.account  # money still goes to the account on file
    assert d["vendor_phones"] == [PHONE]

    url = f"/api/drafts/{draft_id}/approve"
    assert client.post(url, json={}, headers=h).status_code == 409
    for bad, status in (
        (callback(phone="050-1234567"), 409),  # a number from the invoice, not the master
        (callback(person="   "), 409),
        (callback(person=""), 422),
        (callback(minutes_ago=-60), 409),  # in the future
        (callback(minutes_ago=8 * 24 * 60), 409),  # too old
    ):
        r = client.post(url, json={"callback": bad}, headers=h)
        assert r.status_code == status, (bad, r.text)
    r = client.post(url, json={"callback": callback(phone="+972-3-5551234")}, headers=h)
    assert r.status_code == 200, r.text
    done = client.get(f"/api/drafts/{draft_id}").json()
    assert done["callback"]["phone"] == PHONE
    assert done["callback"]["person"] == "Rina, accounts"


def test_the_database_requires_the_callback_even_from_the_api_role(
    env: Env, clean_invoice: ExtractedInvoice
) -> None:
    draft_id = env.receive("doc-check", moved(clean_invoice)).draft_id
    with pytest.raises(IntegrityError, match="bank_change_needs_callback"), env.engine(API_ROLE).begin() as c:
        c.execute(
            text(
                "UPDATE payment_drafts SET state = 'APPROVED', decided_by = "
                "(SELECT id FROM users WHERE username = 'approver'), callback_phone = ' ', "
                "callback_person = 'x', callback_at = now() WHERE id = :id"
            ),
            {"id": draft_id},
        )


def test_a_vendor_master_change_after_drafting_blocks_approval_until_rechecked(
    env: Env, clean_invoice: ExtractedInvoice
) -> None:
    client = env.client
    draft_id = env.receive("doc-master", clean_invoice).draft_id
    admin = login(client, "admin", "admin-pw")
    r = client.post(
        "/api/vendors",
        json={
            "tax_id": ACME_ID,
            "name": "Acme Office Supplies Ltd",
            "bank": {"bank_code": "11", "branch": "100", "account": "5555555"},
            "phones": [PHONE],
        },
        headers=admin,
    )
    assert r.status_code == 201
    h = as_approver(env)
    url = f"/api/drafts/{draft_id}"
    assert client.get(url).json()["master_changed"] is True
    r = client.post(f"{url}/approve", json={}, headers=h)
    assert r.status_code == 409
    assert "re-check" in r.json()["detail"]
    assert client.post(f"{url}/recheck", headers=h).status_code == 200
    d = client.get(url).json()
    assert d["master_changed"] is False
    assert d["pay_to"]["account"] == "5555555"
    assert "BANK_DETAILS_CHANGED" in d["flags"]  # the invoice still shows the old account


# ---- receiving documents -----------------------------------------------------------------------


def test_duplicates_and_extraction_failures_cannot_be_approved(
    env: Env, clean_invoice: ExtractedInvoice
) -> None:
    client = env.client
    admin = login(client, "admin", "admin-pw")
    first = env.fake.register("doc-first", clean_invoice)
    a = upload(client, first, admin)
    again = upload(client, first, admin)
    assert again == {"draft_id": a["draft_id"], "duplicate_upload": True}
    # Same vendor and number written differently, in another file: blocked as a duplicate.
    b = upload(client, env.fake.register("doc-resent", numbered(clean_invoice, "inv 2041")), admin)[
        "draft_id"
    ]
    broken = upload(client, env.fake.register("doc-garbled", None), admin)["draft_id"]
    h = as_approver(env)
    assert client.get(f"/api/drafts/{b}").json()["state"] == "BLOCKED"
    assert client.post(f"/api/drafts/{b}/approve", json={}, headers=h).status_code == 409
    garbled = client.get(f"/api/drafts/{broken}").json()
    assert garbled["state"] == "EXTRACTION_FAILED"
    assert "by hand" not in " ".join(garbled["reasoning"])
    assert client.post(f"/api/drafts/{broken}/approve", json={}, headers=h).status_code == 409
    r = client.post(f"/api/drafts/{broken}/reject", json={"comment": "unreadable"}, headers=h)
    assert r.status_code == 200


def test_a_rejected_invoice_does_not_block_the_genuine_one(env: Env, clean_invoice: ExtractedInvoice) -> None:
    forged = env.receive("doc-fake", clean_invoice).draft_id
    approver = env.api.authenticate("approver", "approve-pw")
    assert approver is not None
    env.api.reject(forged, approver, "forged")
    genuine = env.receive("doc-genuine", clean_invoice).draft_id
    h = as_approver(env)
    d = env.client.get(f"/api/drafts/{genuine}").json()
    assert d["state"] == "NEEDS_ATTENTION"
    assert d["flags"] == ["DUPLICATE_OF_REJECTED"]
    assert env.client.post(f"/api/drafts/{genuine}/approve", json={}, headers=h).status_code == 200


def test_errors_never_leave_a_document_stuck(env: Env, clean_invoice: ExtractedInvoice) -> None:
    content = env.fake.register("doc-flaky", clean_invoice)
    env.fake.fail = RuntimeError("connection reset by peer")
    failed = env.agent.receive(content, "a.pdf", "INBOX", None)
    env.fake.fail = None
    with env.engine(API_ROLE).connect() as conn:
        error = conn.execute(text("SELECT extraction_error FROM invoices")).scalar()
    assert isinstance(error, str)
    assert "reset" not in error  # internals stay in the server log
    assert env.agent.receive(content, "a.pdf", "INBOX", None) == Received(failed.draft_id, True)

    root = create_engine(env.urls["superuser"])

    def store(raw: bytes, source: str) -> int:
        doc = documents.load(raw, "x.pdf")
        with Session(root) as s, s.begin():
            row = DocumentRow(
                sha256=doc.sha256,
                filename="x.pdf",
                media_type=doc.media_type,
                content=doc.content,
                original_sha256=doc.original_sha256,
                preview_png=doc.preview_png,
                source=source,
            )
            s.add(row)
            s.flush()
            return row.id

    # A crash between storing the document and writing its draft: receiving it again finishes the job.
    orphan = env.fake.register("doc-orphan", numbered(clean_invoice, "INV-9"))
    store(orphan, "INBOX")
    assert env.agent.receive(orphan, "o.pdf", "INBOX", None).duplicate_upload is False

    # A document from the paid history (an invoice with no draft) is a conflict, not a crash.
    history = env.fake.register("doc-history", numbered(clean_invoice, "OLD-1"))
    doc_id = store(history, "SEED")
    with Session(root) as s, s.begin():
        s.add(InvoiceRow(document_id=doc_id, model="seed", total=Decimal("1.00")))
    root.dispose()
    admin = login(env.client, "admin", "admin-pw")
    r = env.client.post(
        "/api/documents", files={"file": ("h.pdf", history, "application/pdf")}, headers=admin
    )
    assert r.status_code == 409


def test_a_new_covering_message_on_a_resent_file_is_reassessed(
    env: Env, clean_invoice: ExtractedInvoice
) -> None:
    client = env.client
    admin = login(client, "admin", "admin-pw")
    content = env.fake.register("doc-resend", clean_invoice)
    first = upload(client, content, admin)["draft_id"]
    assert client.get(f"/api/drafts/{first}").json()["state"] == "READY"
    again = upload(client, content, admin, message="Please note: we moved banks, pay the new account.")
    assert again == {"draft_id": first, "duplicate_upload": True}
    d = client.get(f"/api/drafts/{first}").json()
    assert "BANK_CHANGE_REQUESTED" in d["flags"]
    assert d["requires_bank_verification"] is True
    assert "moved banks" in d["document"]["message_text"]
    actions = [e["action"] for e in client.get("/api/audit").json()]
    assert actions[:2] == ["draft.reassessed", "document.duplicate_upload"]


def test_foreign_currency_and_credit_notes_are_blocked(env: Env, clean_invoice: ExtractedInvoice) -> None:
    usd = env.receive("doc-usd", clean_invoice.model_copy(update={"currency": "USD", "invoice_number": "U1"}))
    credit = clean_invoice.model_copy(
        update={
            "invoice_number": "C1",
            "line_items": [],
            "subtotal": Decimal("-1000.00"),
            "vat_amount": Decimal("-180.00"),
            "total": Decimal("-1180.00"),
        }
    )
    negative = env.receive("doc-credit", credit)
    login(env.client, "approver", "approve-pw")
    u = env.client.get(f"/api/drafts/{usd.draft_id}").json()
    assert u["state"] == "BLOCKED"
    assert u["currency"] == "USD"
    assert "currency" in u["failed_checks"]
    n = env.client.get(f"/api/drafts/{negative.draft_id}").json()
    assert n["state"] == "BLOCKED"
    assert n["amount"] is None


# ---- the API's edges ---------------------------------------------------------------------------


def test_sign_in_throttling_is_per_ip_and_sign_out_revokes(env: Env) -> None:
    client = env.client
    assert client.get("/api/drafts").status_code == 401
    assert client.post("/api/session", json={"username": "approver", "password": "x"}).status_code == 403
    stranger = {CLIENT_HEADER: "t", "X-Real-IP": "203.0.113.9"}
    for _ in range(5):
        r = client.post("/api/session", json={"username": "approver", "password": "x"}, headers=stranger)
        assert r.status_code == 401
    r = client.post("/api/session", json={"username": "approver", "password": "approve-pw"}, headers=stranger)
    assert r.status_code == 429
    # The real approver, elsewhere, is not locked out by a stranger's attempts.
    h = login(client, "approver", "approve-pw", ip="10.0.0.2")
    cookie = client.cookies.get("payables_session")
    assert cookie
    assert client.get("/api/me").status_code == 200
    assert client.delete("/api/session", headers=h).status_code == 200
    client.cookies.set("payables_session", cookie)
    assert client.get("/api/me").status_code == 401  # the old cookie no longer works
    r = client.post(
        "/api/session", json={"username": "nobody", "password": "x"}, headers={CLIENT_HEADER: "t"}
    )
    assert r.status_code == 401


def test_api_input_guards(env: Env, clean_invoice: ExtractedInvoice) -> None:
    client = env.client
    h = login(client, "admin", "admin-pw")
    assert client.get(f"/api/drafts/{2**31}").status_code == 422
    assert client.get("/api/audit?limit=-1").status_code == 422
    assert client.get("/api/audit?limit=100000").status_code == 422
    assert client.post("/api/drafts/1/reject", json={"comment": "a\x00b"}, headers=h).status_code == 422
    r = client.post(
        "/api/documents",
        files={"file": ("x.pdf", env.fake.register("doc-nul", clean_invoice), "application/pdf")},
        data={"message_text": "hi\x00"},
        headers=h,
    )
    assert r.status_code == 422
    r = client.post(
        "/api/documents", files={"file": ("x.pdf", b"MZ not a pdf", "application/pdf")}, headers=h
    )
    assert r.status_code == 422
    for bank, message in (
        ({"iban": "NOT-AN-IBAN"}, "IBAN"),
        ({"bank_code": "12345", "branch": "600", "account": "1234"}, "bank code"),
    ):
        r = client.post("/api/vendors", json={"tax_id": ACME_ID, "name": "x", "bank": bank}, headers=h)
        assert r.status_code == 422
        assert message in r.json()["detail"]
    r = client.post("/api/vendors", json={"tax_id": ACME_ID, "name": "x", "phones": ["12"]}, headers=h)
    assert r.status_code == 422
    assert client.post("/api/vendors", json={"tax_id": "123", "name": "x"}, headers=h).status_code == 422
    assert client.get("/api/docs").status_code == 404


def test_downloads_return_the_original_and_previews_are_capped_pngs(env: Env) -> None:
    client = env.client
    h = login(client, "admin", "admin-pw")
    buf = io.BytesIO()
    Image.new("RGB", (3000, 1500), "white").save(buf, format="JPEG")
    jpeg = buf.getvalue()
    r = client.post("/api/documents", files={"file": ("scan.jpg", jpeg, "image/jpeg")}, headers=h)
    assert r.status_code == 201
    doc_id = client.get(f"/api/drafts/{r.json()['draft_id']}").json()["document"]["id"]
    original = client.get(f"/api/documents/{doc_id}/content")
    assert original.content == jpeg
    assert original.headers["content-type"] == "image/jpeg"
    assert original.headers["content-security-policy"].startswith("sandbox")
    assert original.headers["content-disposition"].startswith("attachment")
    preview = client.get(f"/api/documents/{doc_id}/preview")
    assert preview.headers["content-type"] == "image/png"
    with Image.open(io.BytesIO(preview.content)) as img:
        assert max(img.size) <= documents.MAX_SIDE


# ---- audit -------------------------------------------------------------------------------------


def test_audit_log_is_append_only_and_rewrites_without_the_key_are_detected(
    env: Env, clean_invoice: ExtractedInvoice
) -> None:
    for i in range(3):
        env.receive(f"doc-audit-{i}", numbered(clean_invoice, f"A{i}"))
    env.agent.checkpoint()
    for role in (API_ROLE, AGENT_ROLE):
        engine = env.engine(role)
        with pytest.raises(DBAPIError, match=r"append-only|permission denied"), engine.begin() as conn:
            conn.execute(text("UPDATE audit_log SET actor = 'mallory'"))
        with pytest.raises(DBAPIError, match=r"append-only|permission denied"), engine.begin() as conn:
            conn.execute(text("DELETE FROM audit_log"))
        engine.dispose()

    root = create_engine(env.urls["superuser"])

    def verify() -> audit.Verification:
        with Session(root) as s:
            return audit.verify(s, AUDIT_KEY, env.anchors)

    assert verify().intact
    # A superuser disables the trigger, rewrites an entry and recomputes its hash, without the key.
    with root.begin() as conn:
        conn.execute(text("ALTER TABLE audit_log DISABLE TRIGGER audit_log_no_change"))
        conn.execute(
            text(
                'UPDATE audit_log SET details = \'{"amount": "1.00"}\', '
                "hash = encode(sha256(convert_to(prev_hash || 'forged', 'UTF8')), 'hex') WHERE seq = 2"
            )
        )
        conn.execute(text("ALTER TABLE audit_log ENABLE TRIGGER audit_log_no_change"))
    result = verify()
    assert not result.intact
    assert result.first_broken_entry == 2
    assert result.problem == "entry 2 was altered"
    root.dispose()


def test_deleting_the_newest_entries_is_caught_by_the_checkpoint(
    env: Env, clean_invoice: ExtractedInvoice
) -> None:
    for i in range(2):
        env.receive(f"doc-tail-{i}", numbered(clean_invoice, f"T{i}"))
    env.agent.checkpoint()
    root = create_engine(env.urls["superuser"])
    with Session(root) as s:
        head = audit.verify(s, AUDIT_KEY, env.anchors)
    assert head.intact
    # The tail goes, leaving a shorter chain whose every hash is still valid.
    with root.begin() as conn:
        conn.execute(text("ALTER TABLE audit_log DISABLE TRIGGER audit_log_no_change"))
        conn.execute(text("DELETE FROM audit_log WHERE seq > :n"), {"n": head.entries - 3})
        conn.execute(text("ALTER TABLE audit_log ENABLE TRIGGER audit_log_no_change"))
    with Session(root) as s:
        assert audit.verify(s, AUDIT_KEY).intact  # without the checkpoint, nothing looks wrong
        result = audit.verify(s, AUDIT_KEY, env.anchors)
    assert not result.intact
    assert result.problem is not None
    assert "deleted" in result.problem
    root.dispose()


# ---- the inbox ---------------------------------------------------------------------------------


def test_html_email_bank_change_is_flagged_and_bad_files_do_not_stop_the_inbox(
    env: Env, clean_invoice: ExtractedInvoice, tmp_path: Path
) -> None:
    html_mail = EmailMessage()
    html_mail["From"] = "billing@acme.example"
    html_mail["Subject"] = "Invoice"
    html_mail.set_content("<p>Hi,</p><p>please note <b>our account has changed</b>.</p>", subtype="html")
    pdf = env.fake.register("doc-email", clean_invoice)
    html_mail.add_attachment(pdf, maintype="application", subtype="pdf", filename="i.pdf")
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "a-bad-charset.eml").write_bytes(
        b'From: x@y.example\nContent-Type: text/plain; charset="x-no-such-charset"\n\nhello\n'
    )
    (inbox / "b-mail.eml").write_bytes(html_mail.as_bytes())
    (inbox / "c-notes.txt").write_text("not an invoice")

    assert process_inbox(env.agent, inbox) == 1
    assert (inbox / "processed" / "b-mail.eml").exists()
    assert (inbox / "rejected" / "a-bad-charset.eml.reason.txt").exists()
    assert (inbox / "rejected" / "c-notes.txt.reason.txt").exists()
    login(env.client, "approver", "approve-pw")
    draft_id = env.client.get("/api/drafts").json()[0]["id"]
    d = env.client.get(f"/api/drafts/{draft_id}").json()
    assert "BANK_CHANGE_REQUESTED" in d["flags"]
    assert d["requires_bank_verification"] is True
    assert d["document"]["submitted_by_email"] == "billing@acme.example"
    assert d["document"]["uploaded_by"] is None


def test_amounts_are_decimal_strings(env: Env, clean_invoice: ExtractedInvoice) -> None:
    h = login(env.client, "admin", "admin-pw")
    draft_id = upload(env.client, env.fake.register("doc-money", clean_invoice), h)["draft_id"]
    d = env.client.get(f"/api/drafts/{draft_id}").json()
    assert d["amount"] == "1180.00"
    assert Decimal(d["extracted"]["total"]) == Decimal("1180.00")


def test_service_level_callback_rules(env: Env, clean_invoice: ExtractedInvoice) -> None:
    draft_id = env.receive("doc-svc", moved(clean_invoice)).draft_id
    approver = env.api.authenticate("approver", "approve-pw")
    assert approver is not None
    with pytest.raises(Conflict, match="vendor master"):
        env.api.approve(draft_id, approver, None, Callback("09-9999999", "Rina", datetime.now(UTC)))
    assert env.api.approve(draft_id, approver, None, Callback(PHONE, "Rina", datetime.now(UTC))) > 0
