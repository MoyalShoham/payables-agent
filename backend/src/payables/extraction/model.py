"""Turning the model's JSON into a validated ExtractedInvoice."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import ValidationError

from payables.money import parse_amount
from payables.schemas import ExtractedInvoice


@dataclass(frozen=True)
class RawExtraction:
    """What the model returned, as recorded: enough to replay and to audit."""

    text: str
    model: str
    stop_reason: str
    usage: dict[str, int] = field(default_factory=dict)


class ExtractionError(Exception):
    """The model's answer could not be turned into an invoice; the document goes to a person as-is."""


def parse(raw: RawExtraction) -> ExtractedInvoice:
    if raw.stop_reason == "refusal":
        raise ExtractionError("the model declined to process this document")
    if raw.stop_reason == "max_tokens":
        raise ExtractionError("the model's answer was cut off")
    try:
        data: Any = json.loads(raw.text)
    except json.JSONDecodeError as e:
        raise ExtractionError("the model's answer is not JSON") from e
    if not isinstance(data, dict):
        raise ExtractionError("the model's answer is not a JSON object")
    data["vat_rate"] = _rate(data.get("vat_rate"))
    for item in data.get("line_items") or []:
        if isinstance(item, dict):
            item["quantity"] = _decimal(item.get("quantity"))
    try:
        return ExtractedInvoice.model_validate(data)
    except ValidationError as e:
        raise ExtractionError(f"the model's answer does not validate: {e.error_count()} problem(s)") from e


def _rate(value: object) -> Decimal | None:
    """'18%', '18', '0.18' -> Decimal('0.18')."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        d = parse_amount(value.replace("%", ""))
    except ValueError:
        return None
    return d / 100 if d > 1 else d


def _decimal(value: object) -> Decimal | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return Decimal(value.replace(",", "").strip())
    except InvalidOperation:
        return None
