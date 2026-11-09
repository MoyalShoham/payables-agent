"""Risk checks against the vendor master and payment history.

The rule that matters most: bank details on an invoice never change where money goes. Payments use
the account on the vendor master record; an invoice (or email) showing a different account, or bank
details that cannot be compared, raises a flag that always needs a person, who must confirm the
account by phone on a number already on file.

The bank-change wording patterns are a tripwire, not the control: they can be phrased around. The
control is that money only goes to the vendor-master account.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from difflib import SequenceMatcher
from enum import StrEnum

from payables import tax_id
from payables.schemas import BankDetails, ExtractedInvoice, compare_bank

LOOKALIKE_RATIO = 0.85
NEAR_DUPLICATE_DAYS = 7
NEAR_DUPLICATE_SHARE = Decimal("0.01")
ANOMALY_FACTOR = Decimal(3)
ANOMALY_Z = Decimal("3.5")
MAD_TO_SD = Decimal("1.4826")
MIN_HISTORY = 3

# ---- wording that announces new bank details ---------------------------------------------------

_HE = r"(?<![א-ת])[ולבהמש]{0,3}"  # Hebrew one-letter prefixes: ו, ל, ב, ה, מ, ש
_BANKWORD_EN = (
    r"(?:bank(?:ing)?\s+(?:details|account|information|info)|bank\s+account|"
    r"account\s+(?:details|number|info(?:rmation)?)|iban|payment\s+(?:details|instructions|information)|"
    r"remittance\s+(?:details|information|instructions)|wire\s+(?:details|instructions)|bank)"
)
_BANK_CHANGE_PATTERNS = [
    # English
    rf"\b(?:new|updated|changed|revised|different|another)\s+(?:our\s+)?{_BANKWORD_EN}\b",
    r"\bnew\s+account\b(?!\s+(?:manager|executive|representative|rep)\b)",
    rf"\b{_BANKWORD_EN}\s+(?:has|have|had|were|was)\s+(?:been\s+)?(?:changed|updated|moved|replaced)",
    r"\b(?:our|the)\s+(?:bank\s+)?account\s+(?:has|have)\s+(?:been\s+)?(?:changed|updated|moved)",
    r"\b(?:moved|switched|changed)\s+(?:our\s+)?(?:banks\b|to\s+(?:a\s+)?(?:new|another|different)\s+bank)",
    rf"\bchange\s+(?:of|in|to)\s+(?:our\s+)?{_BANKWORD_EN}",
    # Hebrew
    rf"{_HE}חשבון\s+(?:ה?בנק\s+)?(?:ה?חדש|ה?חלופי|ה?מעודכן|אחר)",
    rf"{_HE}פרטי\s+(?:ה?חשבון|ה?בנק|ה?תשלום)(?:\s+ה?בנק)?(?:\s+שלנו)?\s+"
    r"(?:ה?חדשים|השתנו|שונו|עודכנו|התעדכנו|הוחלפו)",
    rf"{_HE}(?:חשבון|בנק)(?:\s+ה?בנק)?(?:\s+שלנו)?\s+(?:השתנה|שונה|הוחלף|עודכן|התחלף)",
    rf"{_HE}(?:שינוי|עדכון|החלפת)\s+(?:[בה]?פרטי\s+)?(?:ה?חשבון|ה?בנק|ה?תשלום)",
    rf"{_HE}(?:עברנו|העברנו|החלפנו|שינינו|עדכנו)\s+(?:את\s+)?(?:ל?בנק|[הל]?חשבון|פרטי\s+ה?(?:בנק|חשבון))",
    rf"{_HE}לעדכן\s+(?:את\s+)?פרטי\s+(?:ה?חשבון|ה?בנק)",
    rf"{_HE}מעבר\s+(?:ל?בנק|חשבון)",
]
_BANK_CHANGE = re.compile("|".join(f"(?:{p})" for p in _BANK_CHANGE_PATTERNS), re.IGNORECASE)

# Text addressed to whoever (or whatever) processes the invoice: a prompt-injection attempt or a
# social-engineering note. The model is told to copy such text into `notes`.
_INSTRUCTIONS = re.compile(
    r"ignore\s+(?:all\s+|any\s+)?(?:your\s+|the\s+)?(?:previous|prior|above|earlier)?\s*instructions"
    r"|\b(?:note|message)\s+to\s+(?:the\s+)?(?:ai|assistant|model|bot|agent)\b"
    r"|\bai\s+assistant\b|\b(?:you\s+are|act\s+as)\s+(?:an?\s+)?(?:ai|assistant)\b"
    r"|\bmark\s+(?:it|this|the\s+invoice)?\s*(?:as\s+)?(?:approved|paid)\b|\bpre-?approved\b"
    r"|התעלם\s+מ?ה?הוראות|סמן\s+(?:אותה\s+|כ)?(?:מאושר|שולם)|אושרה?\s+מראש",
    re.IGNORECASE,
)


def mentions_bank_change(text: str | None) -> bool:
    return bool(text and _BANK_CHANGE.search(text))


def has_instructions(text: str | None) -> bool:
    return bool(text and _INSTRUCTIONS.search(text))


class Flag(StrEnum):
    DUPLICATE_EXACT = "DUPLICATE_EXACT"
    DUPLICATE_SUSPECTED = "DUPLICATE_SUSPECTED"
    DUPLICATE_OF_REJECTED = "DUPLICATE_OF_REJECTED"
    UNKNOWN_VENDOR = "UNKNOWN_VENDOR"
    VENDOR_LOOKALIKE = "VENDOR_LOOKALIKE"
    AMOUNT_ANOMALY = "AMOUNT_ANOMALY"
    BANK_DETAILS_CHANGED = "BANK_DETAILS_CHANGED"
    BANK_DETAILS_UNVERIFIABLE = "BANK_DETAILS_UNVERIFIABLE"
    BANK_CHANGE_REQUESTED = "BANK_CHANGE_REQUESTED"
    INSTRUCTION_TEXT = "INSTRUCTION_TEXT"


class Severity(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"


#: Flags that need the approver to confirm the vendor's bank account by phone, on a number already on
#: file, before approval is possible.
NEEDS_BANK_VERIFICATION = frozenset(
    {Flag.BANK_DETAILS_CHANGED, Flag.BANK_DETAILS_UNVERIFIABLE, Flag.BANK_CHANGE_REQUESTED}
)
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
    #: A person rejected it: a later invoice with the same number may be the genuine one.
    rejected: bool = False


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
    flags.extend(_anomaly(inv, [p for p in past if not p.rejected]))
    if vendor is not None:
        flags.extend(_bank_changed(inv, vendor))
    flags.extend(_bank_change_requested(inv, ctx.message_text))
    flags.extend(_instructions(inv, ctx.message_text))
    return flags


def normalize_name(name: str) -> str:
    s = re.sub(r"[\"'׳״`]", "", name.lower())
    s = re.sub(r"\b(ltd|inc|llc|בע\s*מ|בעמ)\b", " ", s)
    return " ".join(re.sub(r"[^\w\s]", " ", s).split())


def normalize_invoice_number(number: str | None) -> str:
    """ "INV-0042", "inv 42" and "INV42" are the same number: letters and digits only, case-folded,
    leading zeros dropped from each run of digits."""
    s = re.sub(r"[\W_]+", "", (number or "").casefold())
    return re.sub(r"(?<!\d)0+(?=\d)", "", s)


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
    number = normalize_invoice_number(inv.invoice_number)
    same_number = [p for p in past if number and normalize_invoice_number(p.invoice_number) == number]
    if any(not p.rejected for p in same_number):
        p = next(p for p in same_number if not p.rejected)
        return [
            RiskFlag(
                Flag.DUPLICATE_EXACT,
                Severity.HIGH,
                f"invoice {inv.invoice_number} from this vendor was already received ({p.issue_date})",
            )
        ]
    if same_number:
        # Only a rejected invoice had this number. Blocking would let a rejected fake block the
        # genuine invoice, so a person decides.
        return [
            RiskFlag(
                Flag.DUPLICATE_OF_REJECTED,
                Severity.MEDIUM,
                f"an invoice numbered {same_number[0].invoice_number} from this vendor was rejected before",
            )
        ]
    if inv.total is None or inv.issue_date is None:
        return []
    for p in past:
        if p.rejected:
            continue
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
    """Total far above this vendor's usual amounts.

    Distance is measured on log amounts against the median, and the threshold adapts to how much
    this vendor's invoices usually vary: a robust z-score above ANOMALY_Z (median absolute
    deviation scaled to a standard deviation), and never closer than ANOMALY_FACTOR times the median.
    All of it is Decimal arithmetic (Decimal.ln and Decimal.exp), like every other money judgement.
    """
    if inv.total is None or inv.total <= 0 or len(past) < MIN_HISTORY:
        return []
    logs = [p.total.ln() for p in past if p.total > 0]
    if len(logs) < MIN_HISTORY:
        return []
    center = statistics.median(logs)
    spread = MAD_TO_SD * statistics.median([abs(x - center) for x in logs])
    threshold = max(ANOMALY_FACTOR.ln(), ANOMALY_Z * spread)
    distance = inv.total.ln() - center
    # Only larger-than-usual totals: overbilling is the risk; a small invoice is not.
    if distance > threshold:
        median = center.exp().quantize(Decimal("0.01"))
        return [
            RiskFlag(
                Flag.AMOUNT_ANOMALY,
                Severity.MEDIUM,
                f"total {inv.total} is {distance.exp():.1f}x this vendor's usual {median} "
                f"over {len(past)} invoices",
            )
        ]
    return []


def _bank_changed(inv: ExtractedInvoice, vendor: Vendor) -> list[RiskFlag]:
    if inv.bank is None or not inv.bank.has_any():
        return []
    if vendor.bank is None:
        return [
            RiskFlag(
                Flag.BANK_DETAILS_UNVERIFIABLE,
                Severity.HIGH,
                "the invoice prints bank details but the vendor master has no account to compare",
            )
        ]
    status, detail = compare_bank(inv.bank, vendor.bank)
    if status == "different":
        return [
            RiskFlag(Flag.BANK_DETAILS_CHANGED, Severity.HIGH, f"the invoice asks for payment to {detail}")
        ]
    if status == "unverifiable":
        return [RiskFlag(Flag.BANK_DETAILS_UNVERIFIABLE, Severity.HIGH, detail)]
    return []


def _bank_change_requested(inv: ExtractedInvoice, message: str | None) -> list[RiskFlag]:
    for source, text in (("invoice", inv.notes), ("covering message", message)):
        if mentions_bank_change(text):
            return [
                RiskFlag(
                    Flag.BANK_CHANGE_REQUESTED, Severity.HIGH, f"the {source} announces new bank details"
                )
            ]
    return []


def _instructions(inv: ExtractedInvoice, message: str | None) -> list[RiskFlag]:
    for source, text in (("invoice", inv.notes), ("covering message", message)):
        if has_instructions(text):
            return [
                RiskFlag(
                    Flag.INSTRUCTION_TEXT,
                    Severity.MEDIUM,
                    f"the {source} contains text addressed to whoever processes it "
                    "(a prompt-injection or social-engineering attempt?): check every field by hand",
                )
            ]
    return []
