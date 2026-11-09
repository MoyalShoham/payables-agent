"""Extraction with Claude: structured JSON output, PDF and image input."""

from __future__ import annotations

import base64
from typing import Literal, Protocol

import anthropic
from anthropic.types.beta import (
    BetaContentBlockParam,
    BetaImageBlockParam,
    BetaMessageParam,
    BetaOutputConfigParam,
    BetaRequestDocumentBlockParam,
    BetaTextBlockParam,
)

from payables.extraction.documents import SourceDocument
from payables.extraction.model import RawExtraction
from payables.extraction.prompt import SCHEMA, SYSTEM_PROMPT, user_text

DEFAULT_MODEL = "claude-opus-5-5"
Effort = Literal["low", "medium", "high", "xhigh", "max"]
DEFAULT_EFFORT: Effort = "medium"
#: Below nginx's 180 s proxy timeout, so a slow call fails here (EXTRACTION_FAILED) instead of the
#: browser getting a 503 while the draft is still created behind it.
TIMEOUT_SECONDS = 120.0


class Extractor(Protocol):
    def extract(self, doc: SourceDocument, message_text: str | None = None) -> RawExtraction: ...


class ClaudeExtractor:
    """Calls the Messages API. The response format is constrained to SCHEMA, so the text is JSON."""

    def __init__(
        self,
        client: anthropic.Anthropic | None = None,
        model: str = DEFAULT_MODEL,
        effort: Effort = DEFAULT_EFFORT,
    ) -> None:
        self.client = client or anthropic.Anthropic(timeout=TIMEOUT_SECONDS, max_retries=1)
        self.model = model
        self.effort: Effort = effort

    def extract(self, doc: SourceDocument, message_text: str | None = None) -> RawExtraction:
        data = base64.standard_b64encode(doc.content).decode("ascii")
        block: BetaContentBlockParam
        if doc.media_type == "application/pdf":
            pdf: BetaRequestDocumentBlockParam = {
                "type": "document",
                "source": {"type": "base64", "media_type": "application/pdf", "data": data},
            }
            block = pdf
        else:
            image: BetaImageBlockParam = {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": data},
            }
            block = image
        prompt: BetaTextBlockParam = {"type": "text", "text": user_text(message_text)}
        message: BetaMessageParam = {"role": "user", "content": [block, prompt]}
        output_config: BetaOutputConfigParam = {
            "effort": self.effort,
            "format": {"type": "json_schema", "schema": SCHEMA},
        }
        response = self.client.beta.messages.create(
            model=self.model,
            max_tokens=16000,
            system=SYSTEM_PROMPT,
            messages=[message],
            output_config=output_config,
            # If the primary model declines, the API re-runs the request on a fallback model.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        answer = next((b.text for b in response.content if b.type == "text"), "")
        usage = {
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
        }
        return RawExtraction(
            text=answer, model=response.model, stop_reason=str(response.stop_reason), usage=usage
        )
