"""Application wiring: `uvicorn --factory payables.app:make`."""

from __future__ import annotations

import logging

from fastapi import FastAPI

from payables.api import create_app
from payables.config import ExtractionMode, Settings
from payables.db import make_engine, make_sessionmaker
from payables.extraction.claude import ClaudeExtractor, Extractor
from payables.extraction.recording import RecordingExtractor, RecordingStore, ReplayExtractor
from payables.services import Payables


def extractor_for(settings: Settings) -> Extractor:
    store = RecordingStore(settings.recordings_dir)
    match settings.extraction:
        case ExtractionMode.LIVE:
            return ClaudeExtractor()
        case ExtractionMode.RECORD:
            return RecordingExtractor(ClaudeExtractor(), store)
        case ExtractionMode.REPLAY:
            return ReplayExtractor(store)


def build(settings: Settings | None = None) -> tuple[Payables, Settings]:
    """The service for one process. Refuses to start without a usable audit key."""
    settings = settings or Settings()
    service = Payables(
        make_sessionmaker(make_engine(settings.database_url)),
        extractor_for(settings),
        audit_key=settings.require_audit_key(),
        today=settings.today,
        anchors=settings.audit_anchors,
    )
    return service, settings


def make() -> FastAPI:
    logging.basicConfig(level=logging.INFO)
    service, settings = build()
    return create_app(service, settings)
