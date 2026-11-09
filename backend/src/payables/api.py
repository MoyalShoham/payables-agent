"""HTTP API for the review UI.

People sign in with a password and get an HttpOnly cookie holding a signed, random session token;
sessions live in the database, so signing out revokes them. State-changing requests must also send
an X-Payables-Client header (CSRF). The agent account has no password, so it cannot sign in at all.

Each request uses at most one database connection at a time, and no transaction stays open while
the model reads an upload.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterator
from datetime import datetime
from typing import Annotated, Any

from fastapi import Depends, FastAPI, File, Form, HTTPException, Path, Query, Request, Response, UploadFile
from fastapi.responses import JSONResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pydantic import AfterValidator, BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from payables import audit
from payables.config import Settings
from payables.db import AuditEntry, DocumentRow, DraftRow, DraftState, SimulatedPayment, User, VendorRow
from payables.extraction.documents import MAX_BYTES, RejectedDocumentError
from payables.schemas import BankDetails
from payables.services import (
    Callback,
    Conflict,
    Forbidden,
    NotFound,
    Payables,
    draft_detail,
    draft_summary,
    vendor_from_row,
)

log = logging.getLogger(__name__)

COOKIE = "payables_session"
CLIENT_HEADER = "X-Payables-Client"
MAX_FAILED_LOGINS = 5  # per IP and username, per window
MAX_FAILED_PER_IP = 20  # per IP, any username, per window
LOCKOUT_SECONDS = 60
MAX_TRACKED = 10_000  # bounded memory for the throttle

Id = Annotated[int, Path(ge=1, le=2**31 - 1)]


def _no_nul(value: str | None) -> str | None:
    if value is not None and "\x00" in value:
        raise ValueError("text must not contain NUL characters")
    return value


Text = Annotated[str, AfterValidator(_no_nul)]


class Login(BaseModel):
    username: Text = Field(max_length=60)
    password: Text = Field(max_length=200)


class CallbackIn(BaseModel):
    phone: Text = Field(min_length=1, max_length=40)
    person: Text = Field(min_length=1, max_length=120)
    at: datetime


class Approval(BaseModel):
    comment: Text | None = Field(default=None, max_length=1000)
    callback: CallbackIn | None = None


class Rejection(BaseModel):
    comment: Text = Field(min_length=1, max_length=1000)


class VendorIn(BaseModel):
    tax_id: Text = Field(max_length=20)
    name: Text = Field(min_length=1, max_length=200)
    bank: BankDetails | None = None
    phones: list[Text] | None = Field(default=None, max_length=5)


class CsrfMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        unsafe = request.method not in ("GET", "HEAD", "OPTIONS")
        if unsafe and request.url.path.startswith("/api/") and not request.headers.get(CLIENT_HEADER):
            return JSONResponse({"detail": f"missing {CLIENT_HEADER} header"}, status_code=403)
        return await call_next(request)


class LoginThrottle:
    """Failed sign-ins per (IP, username) and per IP, in a bounded LRU. Throttling by IP as well as
    username means a stranger cannot lock a user out from somewhere else."""

    def __init__(self) -> None:
        self._failures: OrderedDict[tuple[str, str], list[float]] = OrderedDict()
        self._lock = threading.Lock()

    def _recent(self, key: tuple[str, str], now: float) -> list[float]:
        times = [t for t in self._failures.get(key, []) if now - t < LOCKOUT_SECONDS]
        if times:
            self._failures[key] = times
            self._failures.move_to_end(key)
        else:
            self._failures.pop(key, None)
        return times

    def blocked(self, ip: str, username: str) -> bool:
        now = time.monotonic()
        with self._lock:
            return (
                len(self._recent((ip, username), now)) >= MAX_FAILED_LOGINS
                or len(self._recent((ip, ""), now)) >= MAX_FAILED_PER_IP
            )

    def failed(self, ip: str, username: str) -> None:
        now = time.monotonic()
        with self._lock:
            for key in ((ip, username), (ip, "")):
                self._failures.setdefault(key, []).append(now)
                self._failures.move_to_end(key)
            while len(self._failures) > MAX_TRACKED:
                self._failures.popitem(last=False)

    def succeeded(self, ip: str, username: str) -> None:
        with self._lock:
            self._failures.pop((ip, username), None)


def client_ip(request: Request) -> str:
    """nginx sets X-Real-IP; the API is reachable only through it (no published port)."""
    return request.headers.get("x-real-ip") or (request.client.host if request.client else "unknown")


def get_session(request: Request) -> Iterator[Session]:
    service: Payables = request.app.state.service
    with service.sessions() as s:
        yield s


def get_user(request: Request) -> User:
    """The signed-in user, detached from any database session (the lookup's connection is returned
    to the pool before the endpoint runs)."""
    settings: Settings = request.app.state.settings
    signer: URLSafeTimedSerializer = request.app.state.signer
    service: Payables = request.app.state.service
    cookie = request.cookies.get(COOKIE)
    if not cookie:
        raise HTTPException(401, "sign in first")
    try:
        data = signer.loads(cookie, max_age=settings.session_hours * 3600)
    except (BadSignature, SignatureExpired) as e:
        raise HTTPException(401, "session expired; sign in again") from e
    token = data.get("sid") if isinstance(data, dict) else None
    user = service.session_user(token) if isinstance(token, str) else None
    if user is None:
        raise HTTPException(401, "session expired; sign in again")
    request.state.session_token = token
    return user


Db = Annotated[Session, Depends(get_session)]
Me = Annotated[User, Depends(get_user)]


def create_app(service: Payables, settings: Settings) -> FastAPI:
    secret = settings.require_session_secret()
    docs = "/api/docs" if settings.api_docs else None
    app = FastAPI(
        title="payables-agent", docs_url=docs, redoc_url=None, openapi_url=docs and "/api/openapi.json"
    )
    app.add_middleware(CsrfMiddleware)
    signer = URLSafeTimedSerializer(secret, salt="session")
    app.state.service = service
    app.state.settings = settings
    app.state.signer = signer
    throttle = LoginThrottle()

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
    def login(body: Login, request: Request, response: Response) -> dict[str, Any]:
        ip = client_ip(request)
        if throttle.blocked(ip, body.username):
            raise HTTPException(429, "too many failed attempts; wait a minute")
        user = service.authenticate(body.username, body.password)
        if user is None:
            throttle.failed(ip, body.username)
            raise HTTPException(401, "wrong username or password")
        throttle.succeeded(ip, body.username)
        token = service.open_session(user, settings.session_hours)
        response.set_cookie(
            COOKIE,
            signer.dumps({"sid": token}),
            max_age=settings.session_hours * 3600,
            httponly=True,
            samesite="strict",
            secure=settings.secure_cookies,
        )
        return _user(user)

    @app.delete("/api/session")
    def logout(request: Request, response: Response, user: Me) -> dict[str, str]:
        service.close_session(request.state.session_token, user)
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
        if message_text is not None and "\x00" in message_text:
            raise HTTPException(422, "the message must not contain NUL characters")
        content = file.file.read(MAX_BYTES + 1)
        try:
            received = guarded(
                lambda: service.receive(content, file.filename or "upload", "UPLOAD", user, message_text)
            )
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
    def draft(draft_id: Id, user: Me, s: Db) -> dict[str, Any]:
        d = s.get(DraftRow, draft_id)
        if d is None:
            raise HTTPException(404, "no such draft")
        return draft_detail(s, d)

    @app.get("/api/documents/{document_id}/preview")
    def document_preview(document_id: Id, user: Me, s: Db) -> Response:
        """A PNG rendered once at ingest. Uploaded PDFs are never handed to the browser to render."""
        png = s.scalar(select(DocumentRow.preview_png).where(DocumentRow.id == document_id))
        if png is None:
            raise HTTPException(404, "no such document")
        return Response(
            png,
            media_type="image/png",
            headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, max-age=3600"},
        )

    @app.get("/api/documents/{document_id}/content")
    def document_content(document_id: Id, user: Me, s: Db) -> Response:
        """The original upload, byte for byte, as a download only, sandboxed in case a browser opens it."""
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
    def approve(draft_id: Id, body: Approval, user: Me) -> dict[str, Any]:
        call = None
        if body.callback is not None:
            call = Callback(body.callback.phone, body.callback.person, body.callback.at)
        payment_id = guarded(lambda: service.approve(draft_id, user, body.comment, call))
        return {"state": "APPROVED", "simulated_payment_id": payment_id}

    @app.post("/api/drafts/{draft_id}/reject")
    def reject(draft_id: Id, body: Rejection, user: Me) -> dict[str, str]:
        guarded(lambda: service.reject(draft_id, user, body.comment))
        return {"state": "REJECTED"}

    @app.post("/api/drafts/{draft_id}/recheck")
    def recheck(draft_id: Id, user: Me) -> dict[str, str]:
        guarded(lambda: service.recheck(draft_id, user))
        return {"status": "re-checked"}

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
                "phones": list(v.phones),
            }
            for v in rows
        ]

    @app.post("/api/vendors", status_code=201)
    def save_vendor(body: VendorIn, user: Me) -> dict[str, int]:
        try:
            vid = guarded(lambda: service.upsert_vendor(user, body.tax_id, body.name, body.bank, body.phones))
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        return {"id": vid}

    @app.get("/api/payments")
    def payments(user: Me, s: Db) -> list[dict[str, Any]]:
        rows = s.scalars(select(SimulatedPayment).order_by(SimulatedPayment.id.desc()).limit(1000))
        return [
            {
                "id": p.id,
                "draft_id": p.draft_id,
                "amount": str(p.amount),
                "currency": p.currency,
                "pay_to": p.pay_to,
                "reference": p.reference,
                "created_at": p.created_at.isoformat(),
                "simulated": True,
            }
            for p in rows
        ]

    @app.get("/api/audit")
    def audit_log(user: Me, s: Db, limit: Annotated[int, Query(ge=1, le=1000)] = 200) -> list[dict[str, Any]]:
        rows = s.scalars(select(AuditEntry).order_by(AuditEntry.seq.desc()).limit(limit))
        return [
            {
                "id": e.seq,
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
        result = audit.verify(s, service.audit_key, service.anchors)
        return {
            "intact": result.intact,
            "entries": result.entries,
            "first_broken_entry": result.first_broken_entry,
            "problem": result.problem,
            "checkpoint": result.checkpoint,
        }

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


def _user(u: User) -> dict[str, Any]:
    return {"id": u.id, "username": u.username, "display_name": u.display_name, "role": u.role.value}
