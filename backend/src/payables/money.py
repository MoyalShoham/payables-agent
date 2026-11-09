"""Money is Decimal with two places, never float."""

from __future__ import annotations

import re
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation

CENT = Decimal("0.01")
_CURRENCY = re.compile(r"[₪$€]|ILS|NIS|USD|EUR|ש\"?ח", re.IGNORECASE)
#: Twelve integer digits: the largest amount NUMERIC(14,2) can store is 999,999,999,999.99.
_NUMBER = re.compile(r"^-?\d{1,12}(\.\d{1,4})?$")

# Israel's standard VAT rate by the date it took effect.
VAT_RATES: tuple[tuple[date, Decimal], ...] = (
    (date(2012, 9, 1), Decimal("0.17")),
    (date(2013, 6, 2), Decimal("0.18")),
    (date(2015, 10, 1), Decimal("0.17")),
    (date(2025, 1, 1), Decimal("0.18")),
)


def money(value: Decimal | int | str) -> Decimal:
    """Round to agorot with banker's rounding. Floats are refused: they cannot represent money."""
    if isinstance(value, float):
        raise TypeError("money must not be a float")
    if isinstance(value, str):
        value = parse_amount(value)
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_EVEN)


def parse_amount(text: str) -> Decimal:
    """Parse '1,234.56', '₪ 1,234.56', '(12.00)' or '12.00-' into a Decimal."""
    s = _CURRENCY.sub("", text).replace(",", "").replace(" ", "").replace(" ", "").strip()
    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative, s = True, s[1:-1]
    elif s.endswith("-"):
        negative, s = True, s[:-1]
    if not _NUMBER.match(s):
        raise ValueError(f"not an amount: {text[:40]!r}")
    try:
        d = Decimal(s)
    except InvalidOperation as e:  # pragma: no cover - the regex already guarantees a number
        raise ValueError(f"not an amount: {text[:40]!r}") from e
    return -d if negative else d


def normalize_currency(value: str | None) -> str:
    """ISO code for what a document prints: "₪", "NIS", 'ש"ח' are all ILS."""
    v = (value or "").strip().upper().replace(" ", "")
    if v in ("", "ILS", "NIS", "₪", 'ש"ח', "ש״ח", "שח", "שקל", "שקלים"):
        return "ILS"
    return v[:3]


def vat_rate_on(day: date) -> Decimal:
    """The Israeli VAT rate in force on ``day`` (the earliest known rate for older dates)."""
    rate = VAT_RATES[0][1]
    for since, r in VAT_RATES:
        if day >= since:
            rate = r
    return rate
