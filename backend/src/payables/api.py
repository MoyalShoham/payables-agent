"""HTTP API for the review UI.

People sign in with a password and get a signed, HttpOnly session cookie. State-changing requests
must also send an X-Payables-Client header (CSRF). The agent account has no password, so it cannot
sign in at all; it works only through the ingestion process.
"""

from __future__ import annotations

import io
import time
from collections import defaultdict
from collections.abc import Callable, Iterator
from typing import Annotated, Any

import pypdfium2 as pdfium
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import JSONResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from payables import audit
from payables.config import Settings
from payables.db import (
    AuditEntry,
    DocumentRow,
    DraftRow,
    DraftState,
    InvoiceRow,
    SimulatedPayment,
    User,
    VendorRow,
)
from payables.extraction.documents import MAX_BYTES, RejectedDocumentError
from payables.schemas import BankDetails
from payables.services import (
    Conflict,
    Forbidden,
    NotFound,
    Payables,
    check_password,
    draft_summary,
    vendor_from_row,
)

COOKIE = "payables_session"
CLIENT_HEADER = "X-Payables-Client"
MAX_FAILED_LOGINS = 5
LOCKOUT_SECONDS = 60


class Login(BaseModel):
    username: str = Field(max_length=60)
    password: str = Field(max_length=200)


class Approval(BaseModel):
    comment: str | None = Field(default=None, max_length=1000)
    bank_verification_note: str | None = Field(default=None, max_length=1000)


class Rejection(BaseModel):
    comment: str = Field(min_length=1, max_length=1000)


class VendorIn(BaseModel):
    tax_id: str = Field(max_length=20)
    name: str = Field(min_length=1, max_length=200)
    bank: BankDetails | None = None


class CsrfMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        unsafe = request.method not in ("GET", "HEAD", "OPTIONS")
        if unsafe and request.url.path.startswith("/api/") and not request.headers.get(CLIENT_HEADER):
            return JSONResponse({"detail": f"missing {CLIENT_HEADER} header"}, status_code=403)
        return await call_next(request)


def get_session(request: Request) -> Iterator[Session]:
    service: Payables = request.app.state.service
    with service.sessions() as s:
        yield s


def get_user(request: Request, s: Annotated[Session, Depends(get_session)]) -> User:
    settings: Settings = request.app.state.settings
    signer: URLSafeTimedSerializer = request.app.state.signer
    token = request.cookies.get(COOKIE)
    if not token:
        raise HTTPException(401, "sign in first")
    try:
        data = signer.loads(token, max_age=settings.session_hours * 3600)
    except (BadSignature, SignatureExpired) as e:
        raise HTTPException(401, "session expired; sign in again") from e
    user = s.get(User, int(data["uid"]))
    if user is None or not user.password_hash:
        raise HTTPException(401, "sign in first")
    return user


Db = Annotated[Session, Depends(get_session)]
Me = Annotated[User, Depends(get_user)]


def create_app(service: Payables, settings: Settings) -> FastAPI:
    app = FastAPI(title="payables-agent", docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.add_middleware(CsrfMiddleware)
    signer = URLSafeTimedSerializer(settings.session_secret, salt="session")
    app.state.service = service
    app.state.settings = settings
    app.state.signer = signer
    failures: dict[str, list[float]] = defaultdict(list)

    def guarded(fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except Forbidden as e:
            raise HTTPException(403, str(e)) from e
        except Conflict as e:
            raise HTTPException(409, str(e)) from e
        except NotFound as e:
            raise HTTPException(404, str(e)) from e

    # ---- session -------------------------------------------------------------------------------

    @app.post("/api/session")
    def login(body: Login, response: Response, s: Db) -> dict[str, Any]:
        now = time.monotonic()
        recent = [t for t in failures[body.username] if now - t < LOCKOUT_SECONDS]
        failures[body.username] = recent
        if len(recent) >= MAX_FAILED_LOGINS:
            raise HTTPException(429, "too many failed attempts; wait a minute")
        user = s.scalars(select(User).where(User.username == body.username)).first()
        if user is None or not check_password(user, body.password):
            failures[body.username].append(now)
            raise HTTPException(401, "wrong username or password")
        failures.pop(body.username, None)
        response.set_cookie(
            COOKIE,
            signer.dumps({"uid": user.id}),
            max_age=settings.session_hours * 3600,
            httponly=True,
            samesite="strict",
            secure=settings.secure_cookies,
        )
        return _user(user)

    @app.delete("/api/session")
    def logout(response: Response) -> dict[str, str]:
        response.delete_cookie(COOKIE)
        return {"status": "signed out"}

    @app.get("/api/me")
    def me(user: Me) -> dict[str, Any]:
        return _user(user)

    # ---- documents and drafts ---------------------------------------------------------------

    @app.post("/api/documents", status_code=201)
    def upload(
        user: Me,
        file: Annotated[UploadFile, File()],
        message_text: Annotated[str | None, Form(max_length=4000)] = None,
    ) -> dict[str, Any]:
        # A sync endpoint runs in the thread pool, so the model call does not block the event loop.
        content = file.file.read(MAX_BYTES + 1)
        try:
            received = service.receive(content, file.filename or "upload", "UPLOAD", user, message_text)
        except RejectedDocumentError as e:
            raise HTTPException(422, str(e)) from e
        return {"draft_id": received.draft_id, "duplicate_upload": received.duplicate_upload}

    @app.get("/api/drafts")
    def drafts(user: Me, s: Db, state: DraftState | None = None) -> list[dict[str, Any]]:
        q = select(DraftRow).order_by(DraftRow.id.desc()).limit(500)
        if state is not None:
            q = q.where(DraftRow.state == state)
        return [draft_summary(s, d) for d in s.scalars(q)]

    @app.get("/api/drafts/{draft_id}")
    def draft(draft_id: int, user: Me, s: Db) -> dict[str, Any]:
        d = s.get(DraftRow, draft_id)
        if d is None:
            raise HTTPException(404, "no such draft")
        invoice = s.get(InvoiceRow, d.invoice_id)
        assert invoice is not None
        doc = s.get(DocumentRow, invoice.document_id)
        assert doc is not None
        payment = s.scalars(select(SimulatedPayment).where(SimulatedPayment.draft_id == d.id)).first()
        names = {u.id: u.display_name for u in s.scalars(select(User))}
        return {
            **draft_summary(s, d),
            "extracted": invoice.extracted,
            "extraction_error": invoice.extraction_error,
            "model": invoice.model,
            "checks": d.checks,
            "flag_details": d.flags,
            "reasoning": d.reasoning,
            "pay_to": d.pay_to,
            "prepared_by": names.get(d.prepared_by),
            "decided_by": names.get(d.decided_by) if d.decided_by else None,
            "decided_at": d.decided_at.isoformat() if d.decided_at else None,
            "decision_comment": d.decision_comment,
            "bank_verification_note": d.bank_verification_note,
            "document": {
                "id": doc.id,
                "filename": doc.filename,
                "media_type": doc.media_type,
                "message_text": doc.message_text,
                "uploaded_by": names.get(doc.uploaded_by or -1),
            },
            "payment": None
            if payment is None
            else {"reference": payment.reference, "amount": str(payment.amount), "simulated": True},
        }

    @app.get("/api/documents/{document_id}/preview")
    def document_preview(document_id: int, user: Me, s: Db) -> Response:
        """A PNG of the document. Uploaded PDFs are never handed to the browser to render."""
        doc = s.get(DocumentRow, document_id)
        if doc is None:
            raise HTTPException(404, "no such document")
        png = preview_png(doc.content, doc.media_type)
        return Response(
            png,
            media_type="image/png",
            headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"},
        )

    @app.get("/api/documents/{document_id}/content")
    def document_content(document_id: int, user: Me, s: Db) -> Response:
        """The original file, as a download only, sandboxed in case a browser opens it anyway."""
        doc = s.get(DocumentRow, document_id)
        if doc is None:
            raise HTTPException(404, "no such document")
        return Response(
            doc.content,
            media_type=doc.media_type,
            headers={
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "sandbox; default-src 'none'",
                "Content-Disposition": f'attachment; filename="document-{doc.id}"',
                "Cache-Control": "private, no-store",
            },
        )

    @app.post("/api/drafts/{draft_id}/approve")
    def approve(draft_id: int, body: Approval, user: Me) -> dict[str, Any]:
        payment_id = guarded(
            lambda: service.approve(draft_id, user, body.comment, body.bank_verification_note)
        )
        return {"state": "APPROVED", "simulated_payment_id": payment_id}

    @app.post("/api/drafts/{draft_id}/reject")
    def reject(draft_id: int, body: Rejection, user: Me) -> dict[str, str]:
        guarded(lambda: service.reject(draft_id, user, body.comment))
        return {"state": "REJECTED"}

    # ---- vendors, payments, audit ------------------------------------------------------------

    @app.get("/api/vendors")
    def vendors(user: Me, s: Db) -> list[dict[str, Any]]:
        rows = s.scalars(select(VendorRow).order_by(VendorRow.name))
        return [
            {
                "id": v.id,
                "tax_id": v.tax_id,
                "name": v.name,
                "bank": b.model_dump() if (b := vendor_from_row(v).bank) else None,
            }
            for v in rows
        ]

    @app.post("/api/vendors", status_code=201)
    def save_vendor(body: VendorIn, user: Me) -> dict[str, int]:
        try:
            vid = guarded(lambda: service.upsert_vendor(user, body.tax_id, body.name, body.bank))
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        return {"id": vid}

    @app.get("/api/payments")
    def payments(user: Me, s: Db) -> list[dict[str, Any]]:
        rows = s.scalars(select(SimulatedPayment).order_by(SimulatedPayment.id.desc()))
        return [
            {
                "id": p.id,
                "draft_id": p.draft_id,
                "amount": str(p.amount),
                "pay_to": p.pay_to,
                "reference": p.reference,
                "created_at": p.created_at.isoformat(),
                "simulated": True,
            }
            for p in rows
        ]

    @app.get("/api/audit")
    def audit_log(user: Me, s: Db, limit: int = 200) -> list[dict[str, Any]]:
        rows = s.scalars(select(AuditEntry).order_by(AuditEntry.id.desc()).limit(min(limit, 1000)))
        return [
            {
                "id": e.id,
                "at": e.at.isoformat(),
                "actor": e.actor,
                "action": e.action,
                "entity": e.entity,
                "entity_id": e.entity_id,
                "details": e.details,
                "hash": e.hash[:12],
            }
            for e in rows
        ]

    @app.get("/api/audit/verify")
    def audit_verify(user: Me, s: Db) -> dict[str, Any]:
        broken = audit.verify(s)
        return {"intact": broken is None, "first_broken_entry": broken}

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


def preview_png(content: bytes, media_type: str) -> bytes:
    """First page of a PDF rasterised server-side; images are already normalised PNGs."""
    if media_type != "application/pdf":
        return content
    pdf = pdfium.PdfDocument(content)
    try:
        page = pdf[0]
        image = page.render(scale=1.5).to_pil()
        out = io.BytesIO()
        image.save(out, format="PNG")
        return out.getvalue()
    finally:
        pdf.close()


def _user(u: User) -> dict[str, Any]:
    return {"id": u.id, "username": u.username, "display_name": u.display_name, "role": u.role.value}
