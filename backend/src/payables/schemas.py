"""What the model extracts from an invoice, as validated domain objects."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator

from payables.money import money, normalize_currency


def _to_money(value: object) -> object:
    if value is None or isinstance(value, Decimal):
        return value if value is None else money(value)
    if isinstance(value, float):
        raise ValueError("amounts must be strings or decimals, not floats")
    if isinstance(value, (int, str)):
        return money(value)
    return value


Money = Annotated[Decimal, BeforeValidator(_to_money)]


class DocumentType(StrEnum):
    TAX_INVOICE = "TAX_INVOICE"  # חשבונית מס
    TAX_INVOICE_RECEIPT = "TAX_INVOICE_RECEIPT"  # חשבונית מס קבלה (already paid)
    PROFORMA = "PROFORMA"  # חשבון עסקה / proforma
    RECEIPT = "RECEIPT"  # קבלה
    QUOTE = "QUOTE"  # הצעת מחיר
    OTHER = "OTHER"


class LineItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    description: str
    quantity: Decimal | None = None
    unit_price: Money | None = None
    amount: Money


def _digits(value: str | None) -> str:
    return "".join(ch for ch in (value or "") if ch.isdigit())


class BankDetails(BaseModel):
    """Israeli bank details: bank code (2 digits), branch (3 digits), account; and/or an IBAN.

    Nothing here is validated, because the model copies whatever an invoice prints. The vendor
    master validates what an admin enters (`validate_master_bank`).
    """

    model_config = ConfigDict(frozen=True)

    bank_name: str | None = None
    bank_code: str | None = None
    branch: str | None = None
    account: str | None = None
    iban: str | None = None

    def iban_compact(self) -> str | None:
        return "".join(self.iban.split()).upper() if self.iban and self.iban.strip() else None

    def local(self) -> tuple[str, str, str] | None:
        """(bank, branch, account) digits without leading zeros, when all three are printed."""
        parts = tuple(_digits(p).lstrip("0") for p in (self.bank_code, self.branch, self.account))
        if all(parts):
            return parts[0], parts[1], parts[2]
        return None

    def local_from_iban(self) -> tuple[str, str, str] | None:
        """An Israeli IBAN encodes the local account: IL kk BBB SSS AAAAAAAAAAAAA."""
        iban = self.iban_compact()
        if not iban or not iban.startswith("IL") or len(iban) != 23 or not iban[2:].isdigit():
            return None
        parts = (iban[4:7].lstrip("0"), iban[7:10].lstrip("0"), iban[10:].lstrip("0"))
        return (parts[0], parts[1], parts[2]) if all(parts) else None

    def has_any(self) -> bool:
        return any(_digits(v) for v in (self.bank_code, self.branch, self.account, self.iban))

    def fingerprint(self) -> str | None:
        """A display form of the account: the IBAN, else bank-branch-account."""
        if iban := self.iban_compact():
            return iban
        local = self.local()
        return "-".join(local) if local else None


def compare_bank(printed: BankDetails, master: BankDetails) -> tuple[str, str]:
    """("same" | "different" | "unverifiable", detail). Every representation the invoice prints
    must match the master: the IBAN against the master's IBAN, the local account against the
    master's local account, or either one against what an Israeli IBAN encodes."""
    if not printed.has_any():
        return "same", "no bank details printed"
    compared = False
    for kind, mine, theirs in (
        ("IBAN", printed.iban_compact(), master.iban_compact()),
        ("account", printed.local(), master.local() or master.local_from_iban()),
        ("account in the IBAN", printed.local_from_iban(), master.local() or master.local_from_iban()),
    ):
        if mine is None or theirs is None:
            continue
        compared = True
        if mine != theirs:
            shown = mine if isinstance(mine, str) else "-".join(mine)
            on_file = theirs if isinstance(theirs, str) else "-".join(theirs)
            return "different", f"{kind} {shown} on the invoice, {on_file} on the vendor master"
    if not compared:
        return (
            "unverifiable",
            "the invoice prints bank details that cannot be compared with the vendor master",
        )
    return "same", "matches the vendor master"


class ExtractedInvoice(BaseModel):
    model_config = ConfigDict(frozen=True)

    vendor_name: str | None = None
    vendor_tax_id: str | None = None
    invoice_number: str | None = None
    document_type: DocumentType = DocumentType.OTHER
    issue_date: date | None = None
    due_date: date | None = None
    currency: str = "ILS"
    line_items: list[LineItem] = Field(default_factory=list)
    subtotal: Money | None = None
    vat_rate: Decimal | None = None
    vat_amount: Money | None = None
    total: Money | None = None
    bank: BankDetails | None = None
    notes: str | None = Field(
        default=None,
        description="Anything on the document a payer should know, e.g. a notice about new bank details.",
    )

    @field_validator("currency", mode="before")
    @classmethod
    def _currency(cls, value: object) -> str:
        return normalize_currency(value if isinstance(value, str) else None)


def iban_is_valid(iban: str) -> bool:
    """ISO 13616 check: rearrange, letters to numbers, the result mod 97 is 1."""
    s = "".join(iban.split()).upper()
    if not (15 <= len(s) <= 34) or not s[:2].isalpha() or not s[2:].isalnum():
        return False
    if s.startswith("IL") and len(s) != 23:
        return False
    rearranged = s[4:] + s[:4]
    digits = "".join(str(int(ch, 36)) for ch in rearranged)
    return int(digits) % 97 == 1


def validate_master_bank(bank: BankDetails) -> BankDetails:
    """What an admin enters on the vendor master must be a well-formed Israeli account or IBAN.
    Raises ValueError with a message for the user."""
    code, branch, account = (_digits(p) for p in (bank.bank_code, bank.branch, bank.account))
    raw = [p for p in (bank.bank_code, bank.branch, bank.account) if p and p.strip()]
    if raw and len(raw) < 3:
        raise ValueError("enter the bank code, branch and account together")
    if raw:
        if any(p.strip() != _digits(p) for p in raw):
            raise ValueError("bank code, branch and account are digits only")
        if len(code.lstrip("0")) != 2 or len(code) > 3:
            raise ValueError("the bank code has 2 digits")
        if len(branch) != 3:
            raise ValueError("the branch has 3 digits")
        if not 4 <= len(account) <= 13:
            raise ValueError("the account number has 4 to 13 digits")
    iban = bank.iban_compact()
    if iban and not iban_is_valid(iban):
        raise ValueError("the IBAN is not valid (check digits)")
    if not raw and not iban:
        raise ValueError("enter an IBAN or the bank code, branch and account")
    checked = BankDetails(
        bank_name=(bank.bank_name or "").strip()[:80] or None,
        bank_code=code.lstrip("0").zfill(2) if raw else None,
        branch=branch or None,
        account=account or None,
        iban=iban,
    )
    if iban and raw:
        status, detail = compare_bank(BankDetails(iban=iban), checked)
        if status == "different":
            raise ValueError("the IBAN and the local account disagree: " + detail)
    return checked
