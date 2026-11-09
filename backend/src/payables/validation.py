"""Checks that do not trust the model: arithmetic, VAT, the tax ID check digit and dates.

Every check is recomputed from the extracted numbers with Decimal arithmetic. A check that cannot run
because a field is missing is SKIP, not PASS, so a sparse extraction never looks clean.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from enum import StrEnum

from payables import tax_id
from payables.money import CENT, money, vat_rate_on
from payables.schemas import DocumentType, ExtractedInvoice

MAX_AGE = timedelta(days=548)  # about 18 months
MAX_TERMS = timedelta(days=180)


class Status(StrEnum):
    PASS = "PASS"  # noqa: S105 - a check status, not a password
    WARN = "WARN"
    FAIL = "FAIL"
    SKIP = "SKIP"


@dataclass(frozen=True)
class Check:
    name: str
    status: Status
    detail: str


def run_checks(inv: ExtractedInvoice, today: date) -> list[Check]:
    return [
        _required_fields(inv),
        _document_type(inv),
        _line_items(inv),
        _vat(inv),
        _total(inv),
        _tax_id(inv),
        _dates(inv, today),
        _currency(inv),
    ]


def has_failure(checks: list[Check]) -> bool:
    return any(c.status is Status.FAIL for c in checks)


def _required_fields(inv: ExtractedInvoice) -> Check:
    missing = [
        name
        for name, value in (
            ("vendor name", inv.vendor_name),
            ("invoice number", inv.invoice_number),
            ("issue date", inv.issue_date),
            ("total", inv.total),
        )
        if value in (None, "")
    ]
    if missing:
        return Check("required_fields", Status.FAIL, "missing: " + ", ".join(missing))
    return Check("required_fields", Status.PASS, "vendor, number, date and total are present")


def _document_type(inv: ExtractedInvoice) -> Check:
    match inv.document_type:
        case DocumentType.TAX_INVOICE:
            return Check("document_type", Status.PASS, "tax invoice")
        case DocumentType.PROFORMA:
            return Check(
                "document_type", Status.WARN, "proforma (חשבון עסקה): a tax invoice should follow payment"
            )
        case DocumentType.TAX_INVOICE_RECEIPT | DocumentType.RECEIPT:
            return Check("document_type", Status.FAIL, "already a receipt: this document says it was paid")
        case DocumentType.QUOTE:
            return Check("document_type", Status.FAIL, "a price quote, not a demand for payment")
        case _:
            return Check("document_type", Status.WARN, "document type not recognised")


def _line_items(inv: ExtractedInvoice) -> Check:
    if not inv.line_items or inv.subtotal is None:
        return Check("line_items_sum", Status.SKIP, "no line items or no subtotal to compare")
    total = money(sum((li.amount for li in inv.line_items), Decimal(0)))
    # Each line was rounded to the agora on the document; allow that rounding per line.
    tolerance = CENT * max(1, len(inv.line_items))
    if abs(total - inv.subtotal) <= tolerance:
        return Check("line_items_sum", Status.PASS, f"{len(inv.line_items)} lines sum to {inv.subtotal}")
    return Check("line_items_sum", Status.FAIL, f"lines sum to {total}, subtotal says {inv.subtotal}")


def _vat(inv: ExtractedInvoice) -> Check:
    if inv.subtotal is None or inv.vat_amount is None or inv.issue_date is None:
        return Check("vat", Status.SKIP, "subtotal, VAT or date missing")
    rate = vat_rate_on(inv.issue_date)
    if inv.vat_amount == 0:
        return Check("vat", Status.WARN, "no VAT charged: only valid for exempt dealers and foreign vendors")
    expected = money(inv.subtotal * rate)
    tolerance = CENT * max(1, len(inv.line_items))
    if abs(expected - inv.vat_amount) <= tolerance:
        return Check("vat", Status.PASS, f"VAT {inv.vat_amount} = {rate:.0%} of {inv.subtotal}")
    return Check(
        "vat",
        Status.FAIL,
        f"VAT {inv.vat_amount} is not {rate:.0%} of {inv.subtotal} "
        f"(expected {expected} at the rate on {inv.issue_date})",
    )


def _total(inv: ExtractedInvoice) -> Check:
    if inv.subtotal is None or inv.vat_amount is None or inv.total is None:
        return Check("total", Status.SKIP, "subtotal, VAT or total missing")
    if abs(inv.subtotal + inv.vat_amount - inv.total) <= CENT:
        return Check("total", Status.PASS, f"{inv.subtotal} + {inv.vat_amount} = {inv.total}")
    return Check("total", Status.FAIL, f"{inv.subtotal} + {inv.vat_amount} is not {inv.total}")


def _tax_id(inv: ExtractedInvoice) -> Check:
    if not inv.vendor_tax_id:
        return Check("tax_id", Status.WARN, "no vendor tax ID on the document")
    if tax_id.is_valid(inv.vendor_tax_id):
        return Check("tax_id", Status.PASS, f"{tax_id.normalize(inv.vendor_tax_id)} has a valid check digit")
    return Check("tax_id", Status.FAIL, f"{inv.vendor_tax_id} fails the Israeli check digit")


def _dates(inv: ExtractedInvoice, today: date) -> Check:
    if inv.issue_date is None:
        return Check("dates", Status.SKIP, "no issue date")
    problems = []
    if inv.issue_date > today + timedelta(days=1):
        problems.append(f"issued in the future ({inv.issue_date})")
    if inv.issue_date < today - MAX_AGE:
        problems.append(f"issued more than 18 months ago ({inv.issue_date})")
    if inv.due_date is not None:
        if inv.due_date < inv.issue_date:
            problems.append("due before it was issued")
        elif inv.due_date > inv.issue_date + MAX_TERMS:
            problems.append("due more than 180 days after issue")
    if problems:
        return Check("dates", Status.FAIL, "; ".join(problems))
    return Check("dates", Status.PASS, "issue and due dates are plausible")


def _currency(inv: ExtractedInvoice) -> Check:
    if inv.currency.upper() in ("ILS", "NIS"):
        return Check("currency", Status.PASS, "ILS")
    return Check("currency", Status.WARN, f"invoice is in {inv.currency}: payment needs a conversion")
