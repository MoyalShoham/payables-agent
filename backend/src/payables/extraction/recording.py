"""Record model responses once, replay them forever.

A recording is keyed by everything that can change the answer: model, effort, the system prompt and
the user text (hashed, so any edit counts, and the covering message is part of the user text), the
schema and the document's bytes. CI and the evaluation replay recordings and never need an API key;
a key mismatch means the prompt or the document changed, and replay fails loudly instead of
silently reusing a stale answer.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path

from payables.extraction.claude import DEFAULT_EFFORT, DEFAULT_MODEL, Effort, Extractor
from payables.extraction.documents import SourceDocument
from payables.extraction.model import RawExtraction
from payables.extraction.prompt import PROMPT_VERSION, prompt_hash, schema_hash


class MissingRecordingError(LookupError):
    pass


def recording_key(
    document_sha256: str,
    message_text: str | None,
    model: str = DEFAULT_MODEL,
    effort: Effort = DEFAULT_EFFORT,
) -> str:
    parts = {
        "model": model,
        "effort": effort,
        "prompt_version": PROMPT_VERSION,
        "prompt": prompt_hash(message_text),
        "schema": schema_hash(),
        "document": document_sha256,
    }
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()[:32]


class RecordingStore:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def path(self, key: str) -> Path:
        return self.directory / f"{key}.json"

    def load(self, key: str) -> RawExtraction | None:
        p = self.path(key)
        if not p.exists():
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
        return RawExtraction(
            text=data["text"],
            model=data["model"],
            stop_reason=data["stop_reason"],
            usage=data.get("usage", {}),
        )

    def save(self, key: str, raw: RawExtraction) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path(key).write_text(
            json.dumps(asdict(raw), ensure_ascii=False, indent=1), encoding="utf-8", newline="\n"
        )


class RecordingExtractor:
    """Uses a stored answer when there is one; otherwise asks the inner extractor and stores it."""

    def __init__(
        self,
        inner: Extractor,
        store: RecordingStore,
        model: str = DEFAULT_MODEL,
        effort: Effort = DEFAULT_EFFORT,
    ) -> None:
        self.inner, self.store, self.model, self.effort = inner, store, model, effort

    def extract(self, doc: SourceDocument, message_text: str | None = None) -> RawExtraction:
        key = recording_key(doc.sha256, message_text, self.model, self.effort)
        stored = self.store.load(key)
        if stored is not None:
            return stored
        raw = self.inner.extract(doc, message_text)
        self.store.save(key, raw)
        return raw


class ReplayExtractor:
    """Only ever returns stored answers. Used by CI and the offline evaluation."""

    def __init__(
        self, store: RecordingStore, model: str = DEFAULT_MODEL, effort: Effort = DEFAULT_EFFORT
    ) -> None:
        self.store, self.model, self.effort = store, model, effort

    def extract(self, doc: SourceDocument, message_text: str | None = None) -> RawExtraction:
        return self.by_hash(doc.sha256, message_text)

    def by_hash(self, document_sha256: str, message_text: str | None = None) -> RawExtraction:
        key = recording_key(document_sha256, message_text, self.model, self.effort)
        stored = self.store.load(key)
        if stored is None:
            raise MissingRecordingError("no recorded model answer for this document and prompt")
        return stored
