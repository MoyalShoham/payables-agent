"""Loads demo data: the synthetic vendor master, payment history and a dozen invoices for review.

    python -m payables.seed

Invoices go through the real pipeline as the agent. With recorded model answers for a document
(the evaluation recordings), those are replayed; otherwise the invoice's ground truth stands in
and the draft says "ground truth (demo, no model call)", so nobody mistakes it for an extraction.
The PDFs replay; the scans do not in the Linux image, whose PNG re-encoding differs from the
macOS/Windows one the recordings were made on, so their hashes find no recording.

Runs with the bootstrap database user (it writes the vendor master and the payment history, which no
application role may do in bulk) and with PAYABLES_TODAY pinned, because the synthetic invoices are
dated mid-2025.
"""

from __future__ import annotations

import json

from sqlalchemy import func, select

from payables import audit, tax_id
from payables.app import build
from payables.db import DocumentRow, InvoiceRow, User, VendorRow
from payables.evaluation import load_manifest
from payables.extraction import documents
from payables.extraction.claude import Extractor
from payables.extraction.documents import SourceDocument
from payables.extraction.model import RawExtraction
from payables.extraction.recording import MissingRecordingError, RecordingStore, ReplayExtractor
from payables.synth.dataset import Render, Sample, build_world
from payables.synth.render import PdfCanvas, draw, render

DEMO_CATEGORIES = [
    "clean",
    "clean",
    "clean",
    "bank_changed_with_notice",
    "bank_change_email",
    "duplicate_exact",
    "amount_anomaly",
    "lookalike_vendor",
    "vat_error",
    "prompt_injection",
    "not_payable",
    "scan_rotated",
]


class DemoExtractor:
    """Replays a recording when there is one, else answers with the sample's ground truth."""

    def __init__(self, replay: ReplayExtractor, truths: dict[str, str]) -> None:
        self.replay, self.truths = replay, truths

    def extract(self, doc: SourceDocument, message_text: str | None = None) -> RawExtraction:
        try:
            return self.replay.extract(doc, message_text)
        except MissingRecordingError:
            return RawExtraction(
                text=self.truths[doc.sha256],
                model="ground truth (demo, no model call)",
                stop_reason="end_turn",
            )


def main() -> None:
    service, settings = build()
    world = build_world()
    with service.sessions.begin() as s:
        if s.scalar(select(func.count()).select_from(VendorRow)):
            print("demo data already loaded")
            return
        admin = s.scalars(select(User).where(User.username == "admin")).one()
        for v in world.vendors:
            assert v.bank is not None
            s.add(
                VendorRow(
                    tax_id=v.tax_id,
                    name=v.name,
                    bank_name=v.bank.bank_name,
                    bank_code=v.bank.bank_code,
                    branch=v.bank.branch,
                    account=v.bank.account,
                    phones=world.phones[v.tax_id],
                )
            )
        # Past invoices: the history the duplicate and anomaly checks compare against.
        for docs in world.history_docs.values():
            for inv in docs:
                sample = Sample(
                    "history", "history", "history", inv, Render("pdf", "he"), None, frozenset(), frozenset()
                )
                pdf = PdfCanvas()
                draw(pdf, sample)
                doc = documents.load(pdf.finish(), f"history-{inv.invoice_number}.pdf")
                row = DocumentRow(
                    sha256=doc.sha256,
                    filename=doc.filename,
                    media_type=doc.media_type,
                    content=doc.content,
                    original_sha256=doc.original_sha256,
                    preview_png=doc.preview_png,
                    source="SEED",
                )
                s.add(row)
                s.flush()
                s.add(
                    InvoiceRow(
                        document_id=row.id,
                        extracted=inv.model_dump(mode="json"),
                        model="seed: payment history",
                        vendor_tax_id=tax_id.normalize(inv.vendor_tax_id or ""),
                        invoice_number=inv.invoice_number,
                        total=inv.total,
                        issue_date=inv.issue_date,
                    )
                )
        audit.append(
            s,
            service.audit_key,
            admin,
            "demo.seeded",
            "vendor",
            None,
            {"vendors": len(world.vendors), "history": sum(len(d) for d in world.history_docs.values())},
        )

    entries = load_manifest("dev")
    chosen = []
    for category in DEMO_CATEGORIES:
        entry = next(e for e in entries if e.sample.category == category and e not in chosen)
        chosen.append(entry)
    truths = {}
    rendered = []
    for e in chosen:
        doc = documents.load(render(e.sample), e.sample.id)
        truths[doc.sha256] = json.dumps(e.sample.truth.model_dump(mode="json"), ensure_ascii=False)
        rendered.append((doc, e))
    extractor: Extractor = DemoExtractor(ReplayExtractor(RecordingStore(settings.recordings_dir)), truths)
    service.extractor = extractor
    for doc, e in rendered:
        service.receive(
            doc.content,
            f"{e.sample.id}.{'pdf' if doc.media_type == 'application/pdf' else 'png'}",
            "MAILBOX" if e.sample.message_text else "INBOX",
            None,
            e.sample.message_text,
        )
    service.checkpoint()
    print(f"loaded {len(world.vendors)} vendors and {len(rendered)} invoices for review")


if __name__ == "__main__":
    main()
