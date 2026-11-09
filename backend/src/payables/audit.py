"""Append-only audit log, chained with an HMAC and checkpointed outside the database.

Each entry has a gap-free sequence number and an HMAC-SHA256, keyed with the audit key, over
(sequence, previous hash, time, actor id, actor, action, entity, details). The key lives outside the
database (a secret file), so:

- the database trigger rejects UPDATE, DELETE and TRUNCATE for every role, and the application roles
  cannot disable it because they do not own the table;
- someone who gets past that (a superuser, a stolen backup restored elsewhere) can delete or edit
  rows, but cannot recompute valid hashes without the key: `verify` names the first bad entry;
- deleting the newest entries leaves a valid shorter chain. Checkpoints catch that: after each
  decision (and from the CLI), the head sequence number and hash are appended to an anchors file
  outside the database, and `verify` requires the chain to still contain the latest checkpoint.
  Entries written after the latest checkpoint can still be truncated unnoticed.

    python -m payables.audit checkpoint     # append the current head to PAYABLES_AUDIT_ANCHORS
    python -m payables.audit verify
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from payables.db import AuditEntry, User

GENESIS = "0" * 64
_LOCK_ID = 7_310_441  # serialises appends so two transactions cannot fork the chain


def _digest(key: bytes, seq: int, prev: str, at: datetime, entry: AuditEntry | dict[str, Any]) -> str:
    get = entry.get if isinstance(entry, dict) else lambda name: getattr(entry, name)
    payload = json.dumps(
        [
            seq,
            prev,
            at.astimezone(UTC).isoformat(),
            get("actor_id"),
            get("actor"),
            get("action"),
            get("entity"),
            get("entity_id"),
            get("details"),
        ],
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )
    return hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()


def append(
    session: Session,
    key: bytes,
    actor: User | None,
    action: str,
    entity: str,
    entity_id: int | None,
    details: dict[str, Any] | None = None,
) -> AuditEntry:
    session.execute(text("SELECT pg_advisory_xact_lock(:id)"), {"id": _LOCK_ID})
    last = session.execute(
        select(AuditEntry.seq, AuditEntry.hash).order_by(AuditEntry.seq.desc()).limit(1)
    ).first()
    seq, prev = (last[0] + 1, last[1]) if last else (1, GENESIS)
    at = datetime.now(UTC)
    fields: dict[str, Any] = {
        "actor_id": actor.id if actor else None,
        "actor": actor.username if actor else "system",
        "action": action,
        "entity": entity,
        "entity_id": entity_id,
        "details": json.loads(json.dumps(details or {}, default=str)),
    }
    entry = AuditEntry(seq=seq, at=at, prev_hash=prev, hash=_digest(key, seq, prev, at, fields), **fields)
    session.add(entry)
    session.flush()
    return entry


@dataclass(frozen=True)
class Verification:
    intact: bool
    entries: int
    #: Sequence number of the first entry that fails, if any.
    first_broken_entry: int | None = None
    problem: str | None = None
    #: The latest checkpoint the chain was checked against.
    checkpoint: dict[str, Any] | None = None


def verify(session: Session, key: bytes, anchors: Path | None = None) -> Verification:
    """Checks every entry's HMAC and link, that sequence numbers have no gaps, and that the latest
    checkpoint is still in the chain."""
    prev, expected_seq, count = GENESIS, 1, 0
    heads: dict[int, str] = {}
    checkpoint = latest_checkpoint(anchors)
    for e in session.scalars(select(AuditEntry).order_by(AuditEntry.seq)):
        count += 1
        if e.seq != expected_seq:
            return Verification(False, count, expected_seq, f"entry {expected_seq} is missing", checkpoint)
        if e.prev_hash != prev or not hmac.compare_digest(e.hash, _digest(key, e.seq, prev, e.at, e)):
            return Verification(False, count, e.seq, f"entry {e.seq} was altered", checkpoint)
        if checkpoint and e.seq == checkpoint["seq"]:
            heads[e.seq] = e.hash
        prev, expected_seq = e.hash, expected_seq + 1
    if checkpoint:
        seq = int(checkpoint["seq"])
        if seq not in heads:
            return Verification(
                False, count, seq, f"the log ends before checkpoint {seq}: entries were deleted", checkpoint
            )
        if not hmac.compare_digest(heads[seq], str(checkpoint["hash"])):
            return Verification(False, count, seq, f"entry {seq} differs from its checkpoint", checkpoint)
    return Verification(True, count, checkpoint=checkpoint)


def checkpoint(session: Session, anchors: Path) -> dict[str, Any] | None:
    """Appends the current head (sequence number and hash) to the anchors file."""
    head = session.execute(
        select(AuditEntry.seq, AuditEntry.hash).order_by(AuditEntry.seq.desc()).limit(1)
    ).first()
    if head is None:
        return None
    latest = latest_checkpoint(anchors)
    if latest and latest["seq"] == head[0]:
        return latest
    line = {"seq": head[0], "hash": head[1], "at": datetime.now(UTC).isoformat()}
    anchors.parent.mkdir(parents=True, exist_ok=True)
    # O_APPEND: concurrent writers (API and agent) each add whole lines.
    fd = os.open(anchors, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    with os.fdopen(fd, "a", encoding="utf-8") as f:
        f.write(json.dumps(line) + "\n")
    return line


def latest_checkpoint(anchors: Path | None) -> dict[str, Any] | None:
    if anchors is None or not anchors.exists():
        return None
    best: dict[str, Any] | None = None
    for raw in anchors.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if (
            isinstance(row, dict)
            and isinstance(row.get("seq"), int)
            and (best is None or row["seq"] > best["seq"])
        ):
            best = row
    return best


def main(argv: list[str] | None = None) -> None:
    from payables.config import Settings
    from payables.db import make_engine, make_sessionmaker

    parser = argparse.ArgumentParser(prog="python -m payables.audit")
    parser.add_argument("command", choices=["checkpoint", "verify"])
    args = parser.parse_args(argv)
    settings = Settings()
    sessions = make_sessionmaker(make_engine(settings.database_url))
    with sessions.begin() as s:
        if args.command == "checkpoint":
            if settings.audit_anchors is None:
                sys.exit("PAYABLES_AUDIT_ANCHORS is not set")
            print(json.dumps(checkpoint(s, settings.audit_anchors)))
        else:
            result = verify(s, settings.require_audit_key(), settings.audit_anchors)
            print(json.dumps(result.__dict__))
            if not result.intact:
                sys.exit(1)


if __name__ == "__main__":
    main()
