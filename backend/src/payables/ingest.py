"""The agent's inbox: a folder of invoice files and saved emails (.eml), processed as they arrive.

    python -m payables.ingest --inbox ./inbox            # process once
    python -m payables.ingest --inbox ./inbox --watch    # keep polling

Each file is moved to inbox/processed/ or, if anything goes wrong with it, to inbox/rejected/ with a
.reason.txt note: one bad file never stops the files after it. For an email, the first PDF or image
attachment is the document; the text body (or the HTML body, as text) is passed along as the
covering message, so "we changed our bank account" in an email is seen by the risk checks, and the
From: address is recorded as the submitter.
"""

from __future__ import annotations

import argparse
import email
import html
import logging
import shutil
import time
from email import policy
from email.message import EmailMessage
from email.utils import parseaddr
from html.parser import HTMLParser
from pathlib import Path

from payables.app import build
from payables.extraction.documents import RejectedDocumentError, sniff
from payables.services import Conflict, Payables

log = logging.getLogger(__name__)

DOCUMENT_SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg", ".webp"}
MAX_EMAIL_BYTES = 25 * 1024 * 1024
MESSAGE_LIMIT = 4000


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in ("br", "p", "div", "li", "tr"):
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_to_text(markup: str) -> str:
    parser = _Text()
    parser.feed(markup)
    parser.close()
    lines = (" ".join(line.split()) for line in html.unescape("".join(parser.parts)).splitlines())
    return "\n".join(line for line in lines if line)


def read_email(raw: bytes) -> tuple[bytes, str, str, str | None]:
    """(attachment bytes, attachment name, body text, From address) of an email; raises if there is no
    document."""
    if len(raw) > MAX_EMAIL_BYTES:
        raise RejectedDocumentError("email is larger than 25 MB")
    msg = email.message_from_bytes(raw, policy=policy.default)
    if not isinstance(msg, EmailMessage):  # pragma: no cover - policy.default always builds one
        raise RejectedDocumentError("not an email")
    body = ""
    plain = msg.get_body(preferencelist=("plain",))
    if plain is not None:
        body = str(plain.get_content())
    else:
        rich = msg.get_body(preferencelist=("html",))
        if rich is not None:
            body = html_to_text(str(rich.get_content()))
    sender = parseaddr(str(msg.get("From", "")))[1] or None
    for part in msg.iter_attachments():
        payload = part.get_payload(decode=True)
        if isinstance(payload, bytes) and sniff(payload) is not None:
            return payload, part.get_filename() or "attachment", body[:MESSAGE_LIMIT], sender
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
                content, name, body, sender = read_email(raw)
                service.receive(content, name, "MAILBOX", None, body or None, submitted_by_email=sender)
            elif path.suffix.lower() in DOCUMENT_SUFFIXES:
                service.receive(raw, path.name, "INBOX", None)
            else:
                raise RejectedDocumentError(f"unsupported file type {path.suffix}")
            shutil.move(path, done / path.name)
            count += 1
        except (RejectedDocumentError, Conflict) as e:
            _reject(path, rejected, str(e))
        except Exception as e:
            # Malformed headers, unknown charsets, a database hiccup: park the file, keep going.
            log.exception("could not process %s", path.name)
            _reject(path, rejected, f"could not be processed ({type(e).__name__}); see the agent log")
    if count:
        service.checkpoint()
    return count


def _reject(path: Path, rejected: Path, reason: str) -> None:
    try:
        shutil.move(path, rejected / path.name)
        (rejected / f"{path.name}.reason.txt").write_text(reason, encoding="utf-8")
    except OSError:
        log.exception("could not move %s out of the inbox", path.name)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO)
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
