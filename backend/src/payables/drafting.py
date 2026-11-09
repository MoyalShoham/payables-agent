"""Turns an extraction, its checks and its risk flags into a payment draft for a person to decide.

The agent never approves. It sorts drafts so a reviewer spends time where it matters, and writes
down why: every line of the reasoning comes from a deterministic check, not from the model.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from payables.risk import BLOCKING, NEEDS_BANK_VERIFICATION, RiskFlag, Severity, Vendor
from payables.schemas import BankDetails, ExtractedInvoice
from payables.validation import Check, Status


class DraftStatus(StrEnum):
    #: Every check passed and no risk was found; still needs a person to approve.
    READY = "READY"
    #: Risk flags or warnings; a person must look before approving.
    NEEDS_ATTENTION = "NEEDS_ATTENTION"
    #: A check failed or the invoice is a duplicate: it cannot be approved, only rejected.
    BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class Draft:
    status: DraftStatus
    reasoning: list[str]
    requires_bank_verification: bool
    #: Where the money would go: the vendor master account, never the one printed on the invoice.
    pay_to: BankDetails | None


def build_draft(
    inv: ExtractedInvoice, checks: list[Check], flags: list[RiskFlag], vendor: Vendor | None
) -> Draft:
    failures = [c for c in checks if c.status is Status.FAIL]
    warnings = [c for c in checks if c.status is Status.WARN]
    blocking = [f for f in flags if f.code in BLOCKING]
    reasoning: list[str] = []
    for c in failures:
        reasoning.append(f"FAILED {c.name}: {c.detail}")
    for f in sorted(flags, key=lambda f: (f.severity is not Severity.HIGH, f.code)):
        reasoning.append(f"{f.severity} {f.code}: {f.detail}")
    for c in warnings:
        reasoning.append(f"Note {c.name}: {c.detail}")
    passed = [c.name for c in checks if c.status is Status.PASS]
    if passed:
        reasoning.append("Passed: " + ", ".join(passed))
    if vendor is None:
        reasoning.append("No vendor master record: there is no verified account to pay to.")
    elif vendor.bank is None:
        reasoning.append("The vendor master has no bank account: add one, verified, before paying.")

    if failures or blocking:
        status = DraftStatus.BLOCKED
    elif flags or warnings:
        status = DraftStatus.NEEDS_ATTENTION
    else:
        status = DraftStatus.READY
    return Draft(
        status=status,
        reasoning=reasoning,
        requires_bank_verification=any(f.code in NEEDS_BANK_VERIFICATION for f in flags),
        pay_to=vendor.bank if vendor else None,
    )
