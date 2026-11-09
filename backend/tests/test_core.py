from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from payables import tax_id
from payables.drafting import DraftStatus, build_draft
from payables.money import money, parse_amount, vat_rate_on
from payables.risk import Flag, RiskContext, Vendor, assess
from payables.schemas import BankDetails, DocumentType, ExtractedInvoice, LineItem
from payables.validation import Status, run_checks

from .conftest import ACME_BANK, ACME_ID, TODAY


def statuses(inv: ExtractedInvoice) -> dict[str, Status]:
    return {c.name: c.status for c in run_checks(inv, TODAY)}


# ---- money and tax IDs ---------------------------------------------------------------------


def test_money_refuses_floats_and_rounds_half_even() -> None:
    with pytest.raises(TypeError):
        money(0.1)  # type: ignore[arg-type]
    assert money("1,234.565") == Decimal("1234.56")
    assert money("₪ 10.005") == Decimal("10.00")
    assert parse_amount("(1,000.00)") == Decimal("-1000.00")
    assert parse_amount("250.00-") == Decimal("-250.00")
    with pytest.raises(ValueError, match="not an amount"):
        parse_amount("9" * 20)


def test_schema_refuses_float_amounts() -> None:
    with pytest.raises(ValidationError):
        ExtractedInvoice(total=1180.0)
    assert ExtractedInvoice(total="1,180.00").total == Decimal("1180.00")


def test_vat_rate_follows_the_israeli_schedule() -> None:
    assert vat_rate_on(date(2024, 12, 31)) == Decimal("0.17")
    assert vat_rate_on(date(2025, 1, 1)) == Decimal("0.18")
    assert vat_rate_on(date(2014, 1, 1)) == Decimal("0.18")


@pytest.mark.parametrize(
    ("value", "valid"),
    [
        ("000000018", True),
        ("123456782", True),
        ("51-345678-0", True),
        ("513456782", False),
        ("000000000", False),
        ("1234", False),
        ("", False),
    ],
)
def test_israeli_check_digit(value: str, valid: bool) -> None:
    assert tax_id.is_valid(value) is valid


def test_check_digit_completion_produces_valid_ids() -> None:
    for prefix in ("51000000", "51234567", "02840193"):
        assert tax_id.is_valid(tax_id.with_check_digit(prefix))


# ---- validation ----------------------------------------------------------------------------


def test_a_clean_invoice_passes_everything(clean_invoice: ExtractedInvoice) -> None:
    assert set(statuses(clean_invoice).values()) == {Status.PASS}


def test_line_items_that_do_not_add_up_fail(clean_invoice: ExtractedInvoice) -> None:
    bad = clean_invoice.model_copy(
        update={
            "line_items": [*clean_invoice.line_items, LineItem(description="Extra", amount=Decimal("50.00"))]
        }
    )
    assert statuses(bad)["line_items_sum"] is Status.FAIL


def test_vat_must_match_the_rate_on_the_invoice_date(clean_invoice: ExtractedInvoice) -> None:
    wrong = clean_invoice.model_copy(update={"vat_amount": Decimal("170.00"), "total": Decimal("1170.00")})
    assert statuses(wrong)["vat"] is Status.FAIL
    # The same 17% is right for an invoice from 2024.
    old = wrong.model_copy(update={"issue_date": date(2024, 12, 20), "due_date": date(2025, 1, 20)})
    assert statuses(old)["vat"] is Status.PASS


def test_total_must_equal_subtotal_plus_vat(clean_invoice: ExtractedInvoice) -> None:
    assert statuses(clean_invoice.model_copy(update={"total": Decimal("1190.00")}))["total"] is Status.FAIL


def test_bad_tax_id_and_implausible_dates_fail(clean_invoice: ExtractedInvoice) -> None:
    s = statuses(
        clean_invoice.model_copy(
            update={
                "vendor_tax_id": "513456782",
                "issue_date": date(2025, 9, 1),
                "due_date": date(2025, 8, 1),
            }
        )
    )
    assert s["tax_id"] is Status.FAIL
    assert s["dates"] is Status.FAIL


def test_missing_fields_are_skipped_or_failed_never_passed() -> None:
    s = statuses(ExtractedInvoice(vendor_name="Someone"))
    assert s["required_fields"] is Status.FAIL
    assert s["vat"] is Status.SKIP
    assert s["total"] is Status.SKIP
    assert Status.PASS not in {s["vat"], s["total"], s["line_items_sum"]}


def test_quotes_and_receipts_are_not_payable(clean_invoice: ExtractedInvoice) -> None:
    for t in (DocumentType.QUOTE, DocumentType.RECEIPT, DocumentType.TAX_INVOICE_RECEIPT):
        assert statuses(clean_invoice.model_copy(update={"document_type": t}))["document_type"] is Status.FAIL


# ---- risk ------------------------------------------------------------------------------------


def codes(inv: ExtractedInvoice, ctx: RiskContext) -> set[Flag]:
    return {f.code for f in assess(inv, ctx)}


def test_a_clean_invoice_from_a_known_vendor_has_no_flags(
    clean_invoice: ExtractedInvoice, context: RiskContext
) -> None:
    assert codes(clean_invoice, context) == set()


def test_resubmitted_invoice_number_is_an_exact_duplicate(
    clean_invoice: ExtractedInvoice, context: RiskContext
) -> None:
    assert Flag.DUPLICATE_EXACT in codes(
        clean_invoice.model_copy(update={"invoice_number": "inv-2021 "}), context
    )


def test_same_amount_days_apart_under_a_new_number_is_a_suspected_duplicate(
    clean_invoice: ExtractedInvoice, context: RiskContext
) -> None:
    near = clean_invoice.model_copy(
        update={"invoice_number": "INV-9999", "issue_date": date(2025, 4, 6), "total": Decimal("1255.00")}
    )
    assert Flag.DUPLICATE_SUSPECTED in codes(near, context)


def test_changed_bank_details_are_flagged(clean_invoice: ExtractedInvoice, context: RiskContext) -> None:
    moved = clean_invoice.model_copy(
        update={"bank": BankDetails(bank_code="10", branch="800", account="99887766")}
    )
    assert codes(moved, context) == {Flag.BANK_DETAILS_CHANGED}
    # The same account written differently is not a change.
    same = clean_invoice.model_copy(
        update={"bank": BankDetails(bank_code="012", branch="600", account="0123456")}
    )
    assert codes(same, context) == set()


@pytest.mark.parametrize(
    "text",
    [
        "Please note our new bank account details below",
        "Our bank details have changed",
        "שימו לב: חשבון בנק חדש",
        "עדכון פרטי חשבון - נא לשלם לחשבון החדש",
    ],
)
def test_bank_change_language_in_notes_or_messages_is_flagged(
    clean_invoice: ExtractedInvoice, context: RiskContext, text: str
) -> None:
    assert Flag.BANK_CHANGE_REQUESTED in codes(clean_invoice.model_copy(update={"notes": text}), context)
    ctx = RiskContext(vendors=context.vendors, history=context.history, message_text=text)
    assert Flag.BANK_CHANGE_REQUESTED in codes(clean_invoice, ctx)


def test_unknown_vendor_and_lookalike(clean_invoice: ExtractedInvoice, context: RiskContext) -> None:
    stranger = clean_invoice.model_copy(
        update={"vendor_name": "Totally New Co", "vendor_tax_id": "510000011"}
    )
    assert codes(stranger, context) == {Flag.UNKNOWN_VENDOR}
    impostor = clean_invoice.model_copy(
        update={"vendor_name": "Acme Ofice Supplies Ltd", "vendor_tax_id": "510000011"}
    )
    assert codes(impostor, context) == {Flag.VENDOR_LOOKALIKE}


def test_amount_far_from_the_vendors_history_is_an_anomaly(
    clean_invoice: ExtractedInvoice, context: RiskContext
) -> None:
    big = clean_invoice.model_copy(
        update={"subtotal": Decimal("5000.00"), "vat_amount": Decimal("900.00"), "total": Decimal("5900.00")}
    )
    assert Flag.AMOUNT_ANOMALY in codes(big, context)
    few = RiskContext(vendors=context.vendors, history=context.history[:2])
    assert Flag.AMOUNT_ANOMALY not in codes(big, few)


# ---- drafting --------------------------------------------------------------------------------


def test_drafts_pay_the_master_account_and_sort_by_risk(
    clean_invoice: ExtractedInvoice, context: RiskContext
) -> None:
    vendor = context.vendors[0]
    ready = build_draft(
        clean_invoice, run_checks(clean_invoice, TODAY), assess(clean_invoice, context), vendor
    )
    assert ready.status is DraftStatus.READY
    assert not ready.requires_bank_verification

    moved = clean_invoice.model_copy(update={"bank": BankDetails(bank_code="10", branch="800", account="1")})
    draft = build_draft(moved, run_checks(moved, TODAY), assess(moved, context), vendor)
    assert draft.status is DraftStatus.NEEDS_ATTENTION
    assert draft.requires_bank_verification
    assert draft.pay_to == ACME_BANK
    assert draft.reasoning[0].startswith("HIGH BANK_DETAILS_CHANGED")

    dup = clean_invoice.model_copy(update={"invoice_number": "INV-2001"})
    assert (
        build_draft(dup, run_checks(dup, TODAY), assess(dup, context), vendor).status is DraftStatus.BLOCKED
    )

    broken = clean_invoice.model_copy(update={"total": Decimal("1.00")})
    assert build_draft(broken, run_checks(broken, TODAY), [], vendor).status is DraftStatus.BLOCKED


def test_unknown_vendor_has_nowhere_verified_to_pay(clean_invoice: ExtractedInvoice) -> None:
    draft = build_draft(clean_invoice, run_checks(clean_invoice, TODAY), [], None)
    assert draft.pay_to is None
    assert any("No vendor master record" in r for r in draft.reasoning)


def test_vendor_ids_compare_after_normalisation(clean_invoice: ExtractedInvoice) -> None:
    ctx = RiskContext(vendors=[Vendor(tax_id="51-345678-0", name="Acme", bank=ACME_BANK)])
    assert Flag.UNKNOWN_VENDOR not in codes(clean_invoice.model_copy(update={"vendor_tax_id": ACME_ID}), ctx)
