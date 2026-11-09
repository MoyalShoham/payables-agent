"""Append-only, hash-chained audit log.

Each entry stores the hash of the previous entry and its own hash over (previous hash, time, actor,
action, entity, details). Rewriting any past entry breaks every hash after it, and `verify` finds the
first broken link. A database trigger additionally rejects UPDATE, DELETE and TRUNCATE.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from payables.db import AuditEntry, User

GENESIS = "0" * 64
_LOCK_ID = 7_310_441  # serialises appends so two transactions cannot fork the chain


def _digest(
    prev: str,
    at: datetime,
    actor: str,
    action: str,
    entity: str,
    entity_id: int | None,
    details: dict[str, Any],
) -> str:
    payload = json.dumps(
        [prev, at.isoformat(), actor, action, entity, entity_id, details],
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def append(
    session: Session,
    actor: User | None,
    action: str,
    entity: str,
    entity_id: int | None,
    details: dict[str, Any] | None = None,
) -> AuditEntry:
    session.execute(text("SELECT pg_advisory_xact_lock(:id)"), {"id": _LOCK_ID})
    prev = session.scalars(select(AuditEntry.hash).order_by(AuditEntry.id.desc()).limit(1)).first() or GENESIS
    at = datetime.now(UTC)
    who = actor.username if actor else "system"
    body = json.loads(json.dumps(details or {}, default=str))
    entry = AuditEntry(
        at=at,
        actor_id=actor.id if actor else None,
        actor=who,
        action=action,
        entity=entity,
        entity_id=entity_id,
        details=body,
        prev_hash=prev,
        hash=_digest(prev, at, who, action, entity, entity_id, body),
    )
    session.add(entry)
    session.flush()
    return entry


def verify(session: Session) -> int | None:
    """Id of the first entry whose chain is broken, or None if the whole log is intact."""
    prev = GENESIS
    for e in session.scalars(select(AuditEntry).order_by(AuditEntry.id)):
        expected = _digest(prev, e.at.astimezone(UTC), e.actor, e.action, e.entity, e.entity_id, e.details)
        if e.prev_hash != prev or e.hash != expected:
            return e.id
        prev = e.hash
    return None
