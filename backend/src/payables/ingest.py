"""The agent's inbox: a folder of invoice files and saved emails (.eml), processed as they arrive.

    python -m payables.ingest --inbox ./inbox            # process once
    python -m payables.ingest --inbox ./inbox --watch    # keep polling

Each file is moved to inbox/processed/ or, if it is rejected, to inbox/rejected/ with a note. For an
email, the first PDF or image attachment is the document and the text body is passed along as the
covering message, so "we changed our bank account" in an email is seen by the risk checks.
"""

from __future__ import annotations

import argparse
import email
import shutil
import time
from email import policy
from email.message import EmailMessage
from pathlib import Path

from payables.app import build
from payables.extraction.documents import RejectedDocumentError, sniff
from payables.services import Payables

DOCUMENT_SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg", ".webp"}
MAX_EMAIL_BYTES = 25 * 1024 * 1024


def read_email(raw: bytes) -> tuple[bytes, str, str]:
    """(attachment bytes, attachment name, plain-text body) of an email; raises if there is no document."""
    if len(raw) > MAX_EMAIL_BYTES:
        raise RejectedDocumentError("email is larger than 25 MB")
    msg = email.message_from_bytes(raw, policy=policy.default)
    if not isinstance(msg, EmailMessage):  # pragma: no cover - policy.default always builds one
        raise RejectedDocumentError("not an email")
    body_part = msg.get_body(preferencelist=("plain",))
    body = body_part.get_content() if body_part is not None else ""
    for part in msg.iter_attachments():
        payload = part.get_payload(decode=True)
        if isinstance(payload, bytes) and sniff(payload) is not None:
            return payload, part.get_filename() or "attachment", str(body)[:4000]
    raise RejectedDocumentError("the email has no PDF or image attachment")


def process_inbox(service: Payables, inbox: Path) -> int:
    done, rejected = inbox / "processed", inbox / "rejected"
    done.mkdir(parents=True, exist_ok=True)
    rejected.mkdir(parents=True, exist_ok=True)
    count = 0
    for path in sorted(p for p in inbox.iterdir() if p.is_file() and not p.name.startswith(".")):
        try:
            raw = path.read_bytes()
            if path.suffix.lower() == ".eml":
                content, name, body = read_email(raw)
                service.receive(content, name, "MAILBOX", None, message_text=body or None)
            elif path.suffix.lower() in DOCUMENT_SUFFIXES:
                service.receive(raw, path.name, "INBOX", None)
            else:
                raise RejectedDocumentError(f"unsupported file type {path.suffix}")
            shutil.move(path, done / path.name)
            count += 1
        except RejectedDocumentError as e:
            shutil.move(path, rejected / path.name)
            (rejected / f"{path.name}.reason.txt").write_text(str(e), encoding="utf-8")
    return count


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m payables.ingest")
    parser.add_argument("--inbox", type=Path, required=True)
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--interval", type=float, default=5.0)
    args = parser.parse_args(argv)
    service, _ = build()
    while True:
        n = process_inbox(service, args.inbox)
        if n:
            print(f"processed {n} file(s)", flush=True)
        if not args.watch:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
