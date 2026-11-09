from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from payables.risk import PastInvoice, RiskContext, Vendor
from payables.schemas import BankDetails, DocumentType, ExtractedInvoice, LineItem

TODAY = date(2025, 6, 15)
ACME_ID = "513456780"  # valid check digit
ACME_BANK = BankDetails(bank_name="Hapoalim", bank_code="12", branch="600", account="123456")


@pytest.fixture
def clean_invoice() -> ExtractedInvoice:
    return ExtractedInvoice(
        vendor_name="Acme Office Supplies Ltd",
        vendor_tax_id=ACME_ID,
        invoice_number="INV-2041",
        document_type=DocumentType.TAX_INVOICE,
        issue_date=date(2025, 6, 1),
        due_date=date(2025, 7, 1),
        line_items=[
            LineItem(
                description="Paper A4 x 10",
                quantity=Decimal(10),
                unit_price=Decimal("25.50"),
                amount=Decimal("255.00"),
            ),
            LineItem(
                description="Toner",
                quantity=Decimal(2),
                unit_price=Decimal("372.50"),
                amount=Decimal("745.00"),
            ),
        ],
        subtotal=Decimal("1000.00"),
        vat_rate=Decimal("0.18"),
        vat_amount=Decimal("180.00"),
        total=Decimal("1180.00"),
        bank=ACME_BANK,
    )


@pytest.fixture
def context() -> RiskContext:
    return RiskContext(
        vendors=[
            Vendor(tax_id=ACME_ID, name="Acme Office Supplies Ltd", bank=ACME_BANK),
            Vendor(tax_id="510000003", name="Galil Cleaning Services"),
        ],
        history=[
            PastInvoice(ACME_ID, f"INV-20{n}", Decimal(total), date(2025, m, 3))
            for n, total, m in (
                ("01", "1100.00", 1),
                ("11", "1300.00", 2),
                ("21", "950.00", 3),
                ("31", "1250.00", 4),
            )
        ],
    )
