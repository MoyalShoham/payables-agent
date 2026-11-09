"""What the model extracts from an invoice, as validated domain objects."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

from payables.money import money


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


class BankDetails(BaseModel):
    """Israeli bank details: bank code (2 digits), branch (3 digits), account; or an IBAN."""

    model_config = ConfigDict(frozen=True)

    bank_name: str | None = None
    bank_code: str | None = None
    branch: str | None = None
    account: str | None = None
    iban: str | None = None

    def fingerprint(self) -> str | None:
        """Comparable identity of the account: the IBAN, else bank-branch-account digits."""
        if self.iban:
            return "".join(self.iban.split()).upper()
        if self.bank_code and self.branch and self.account:
            digits = [
                "".join(ch for ch in part if ch.isdigit()).lstrip("0")
                for part in (self.bank_code, self.branch, self.account)
            ]
            if all(digits):
                return "-".join(digits)
        return None


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
