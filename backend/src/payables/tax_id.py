"""Israeli tax identifiers: company number (ח.פ.), dealer number (ע.מ.) and ID number (ת.ז.).

All three use the same check digit: nine digits (left-padded with zeros), weights alternating 1 and
2, each product's digits summed, and the total divisible by 10.
"""

from __future__ import annotations

import re

_NON_DIGIT = re.compile(r"\D")


def normalize(value: str) -> str:
    """Digits only, left-padded to nine. Empty if there are no digits or too many."""
    digits = _NON_DIGIT.sub("", value)
    if not 5 <= len(digits) <= 9:
        return ""
    return digits.zfill(9)


def is_valid(value: str) -> bool:
    digits = normalize(value)
    if not digits or digits == "000000000":
        return False
    total = 0
    for i, ch in enumerate(digits):
        product = int(ch) * (1 if i % 2 == 0 else 2)
        total += product if product < 10 else product - 9
    return total % 10 == 0


def with_check_digit(first_eight: str) -> str:
    """Complete an eight-digit prefix with its check digit (used by the synthetic data generator)."""
    if len(first_eight) != 8 or not first_eight.isdigit():
        raise ValueError("need exactly eight digits")
    for d in "0123456789":
        if is_valid(first_eight + d):
            return first_eight + d
    raise AssertionError("unreachable: one check digit always works")  # pragma: no cover
