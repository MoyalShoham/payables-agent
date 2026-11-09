"""Application wiring: `uvicorn --factory payables.app:make`."""

from __future__ import annotations

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
    settings = settings or Settings()
    service = Payables(make_sessionmaker(make_engine(settings.database_url)), extractor_for(settings))
    service.ensure_users(settings.approver_password, settings.admin_password)
    return service, settings


def make() -> FastAPI:
    service, settings = build()
    return create_app(service, settings)
