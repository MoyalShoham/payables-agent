"""Risk checks against the vendor master and payment history.

The rule that matters most: bank details on an invoice never change where money goes. Payments use
the account on the vendor master record; an invoice (or email) showing a different account raises a
flag that always needs a person, who must confirm the change through a known channel.
"""

from __future__ import annotations

import math
import re
import statistics
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from difflib import SequenceMatcher
from enum import StrEnum

from payables import tax_id
from payables.schemas import BankDetails, ExtractedInvoice

LOOKALIKE_RATIO = 0.85
NEAR_DUPLICATE_DAYS = 7
NEAR_DUPLICATE_SHARE = Decimal("0.01")
ANOMALY_FACTOR = 3.0
MIN_HISTORY = 3

_BANK_CHANGE = re.compile(
    r"(new|updated?|change[ds]?|changing)\s+(our\s+)?(bank|account|banking)"
    r"|(bank|account)\s+(details|number)\s+(have|has)\s+changed"
    r"|חשבון\s+(בנק\s+)?חדש|שינוי\s+(פרטי\s+)?(חשבון|בנק)|פרטי\s+(ה)?בנק\s+השתנו|עדכון\s+פרטי\s+(ה)?(חשבון|בנק)",
    re.IGNORECASE,
)


class Flag(StrEnum):
    DUPLICATE_EXACT = "DUPLICATE_EXACT"
    DUPLICATE_SUSPECTED = "DUPLICATE_SUSPECTED"
    UNKNOWN_VENDOR = "UNKNOWN_VENDOR"
    VENDOR_LOOKALIKE = "VENDOR_LOOKALIKE"
    AMOUNT_ANOMALY = "AMOUNT_ANOMALY"
    BANK_DETAILS_CHANGED = "BANK_DETAILS_CHANGED"
    BANK_CHANGE_REQUESTED = "BANK_CHANGE_REQUESTED"


class Severity(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"


#: Flags that need the approver to confirm the vendor's bank account by phone, on a number already on
#: file, before approval is possible.
NEEDS_BANK_VERIFICATION = frozenset({Flag.BANK_DETAILS_CHANGED, Flag.BANK_CHANGE_REQUESTED})
#: Flags that block approval outright.
BLOCKING = frozenset({Flag.DUPLICATE_EXACT})


@dataclass(frozen=True)
class RiskFlag:
    code: Flag
    severity: Severity
    detail: str


@dataclass(frozen=True)
class Vendor:
    tax_id: str
    name: str
    bank: BankDetails | None = None


@dataclass(frozen=True)
class PastInvoice:
    vendor_tax_id: str
    invoice_number: str
    total: Decimal
    issue_date: date


@dataclass(frozen=True)
class RiskContext:
    vendors: list[Vendor]
    history: list[PastInvoice] = field(default_factory=list)
    message_text: str | None = None


def assess(inv: ExtractedInvoice, ctx: RiskContext) -> list[RiskFlag]:
    flags: list[RiskFlag] = []
    vendor = _find_vendor(inv, ctx.vendors)
    if vendor is None:
        flags.extend(_unknown_vendor(inv, ctx.vendors))
    vendor_id = vendor.tax_id if vendor else tax_id.normalize(inv.vendor_tax_id or "")
    past = [p for p in ctx.history if vendor_id and tax_id.normalize(p.vendor_tax_id) == vendor_id]
    flags.extend(_duplicates(inv, past))
    flags.extend(_anomaly(inv, past))
    if vendor is not None:
        flags.extend(_bank_changed(inv, vendor))
    flags.extend(_bank_change_requested(inv, ctx.message_text))
    return flags


def normalize_name(name: str) -> str:
    s = re.sub(r"[\"'׳״`]", "", name.lower())
    s = re.sub(r"\b(ltd|inc|llc|בע\s*מ|בעמ)\b", " ", s)
    return " ".join(re.sub(r"[^\w\s]", " ", s).split())


def _find_vendor(inv: ExtractedInvoice, vendors: list[Vendor]) -> Vendor | None:
    wanted = tax_id.normalize(inv.vendor_tax_id or "")
    if wanted:
        return next((v for v in vendors if tax_id.normalize(v.tax_id) == wanted), None)
    # No tax ID on the document: fall back to an exact (normalised) name match.
    name = normalize_name(inv.vendor_name or "")
    return next((v for v in vendors if name and normalize_name(v.name) == name), None)


def _unknown_vendor(inv: ExtractedInvoice, vendors: list[Vendor]) -> list[RiskFlag]:
    name = normalize_name(inv.vendor_name or "")
    best: tuple[float, Vendor] | None = None
    for v in vendors:
        ratio = SequenceMatcher(None, name, normalize_name(v.name)).ratio()
        if best is None or ratio > best[0]:
            best = (ratio, v)
    if best is not None and best[0] >= LOOKALIKE_RATIO:
        return [
            RiskFlag(
                Flag.VENDOR_LOOKALIKE,
                Severity.HIGH,
                f"'{inv.vendor_name}' looks like known vendor '{best[1].name}' but the tax ID "
                f"{inv.vendor_tax_id or '(none)'} is not theirs ({best[1].tax_id})",
            )
        ]
    return [
        RiskFlag(
            Flag.UNKNOWN_VENDOR,
            Severity.MEDIUM,
            f"no vendor with tax ID {inv.vendor_tax_id or '(none)'} on the vendor master",
        )
    ]


def _duplicates(inv: ExtractedInvoice, past: list[PastInvoice]) -> list[RiskFlag]:
    number = (inv.invoice_number or "").strip().lower()
    for p in past:
        if number and p.invoice_number.strip().lower() == number:
            return [
                RiskFlag(
                    Flag.DUPLICATE_EXACT,
                    Severity.HIGH,
                    f"invoice {inv.invoice_number} from this vendor was already received ({p.issue_date})",
                )
            ]
    if inv.total is None or inv.issue_date is None:
        return []
    for p in past:
        close_amount = abs(p.total - inv.total) <= NEAR_DUPLICATE_SHARE * max(p.total, inv.total)
        close_date = abs((p.issue_date - inv.issue_date).days) <= NEAR_DUPLICATE_DAYS
        if close_amount and close_date:
            return [
                RiskFlag(
                    Flag.DUPLICATE_SUSPECTED,
                    Severity.MEDIUM,
                    f"invoice {p.invoice_number} for {p.total} on {p.issue_date} is nearly identical",
                )
            ]
    return []


def _anomaly(inv: ExtractedInvoice, past: list[PastInvoice]) -> list[RiskFlag]:
    if inv.total is None or inv.total <= 0 or len(past) < MIN_HISTORY:
        return []
    median = Decimal(statistics.median(p.total for p in past))
    if median <= 0:
        return []
    ratio = float(inv.total / median)
    if abs(math.log(ratio)) > math.log(ANOMALY_FACTOR):
        return [
            RiskFlag(
                Flag.AMOUNT_ANOMALY,
                Severity.MEDIUM,
                f"total {inv.total} is {ratio:.1f}x this vendor's median of {median:.2f} "
                f"over {len(past)} invoices",
            )
        ]
    return []


def _bank_changed(inv: ExtractedInvoice, vendor: Vendor) -> list[RiskFlag]:
    if inv.bank is None or vendor.bank is None:
        return []
    on_invoice = inv.bank.fingerprint()
    on_file = vendor.bank.fingerprint()
    if on_invoice and on_file and on_invoice != on_file:
        return [
            RiskFlag(
                Flag.BANK_DETAILS_CHANGED,
                Severity.HIGH,
                f"the invoice asks for payment to {on_invoice}; the vendor master has {on_file}",
            )
        ]
    return []


def _bank_change_requested(inv: ExtractedInvoice, message: str | None) -> list[RiskFlag]:
    for source, text in (("invoice", inv.notes), ("message", message)):
        if text and _BANK_CHANGE.search(text):
            return [
                RiskFlag(
                    Flag.BANK_CHANGE_REQUESTED,
                    Severity.HIGH,
                    f"the {source} announces new bank details",
                )
            ]
    return []
