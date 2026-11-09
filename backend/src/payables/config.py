"""Settings from the environment (and a .env file in development)."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class ExtractionMode(StrEnum):
    #: Call Claude for every new document.
    LIVE = "live"
    #: Call Claude once per document and keep the answer (development).
    RECORD = "record"
    #: Only stored answers; no API key needed (tests, CI, the offline demo).
    REPLAY = "replay"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PAYABLES_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://payables:payables@localhost:5432/payables"
    extraction: ExtractionMode = ExtractionMode.REPLAY
    recordings_dir: Path = Path(__file__).resolve().parents[2] / "eval" / "recordings"
    session_secret: str = "dev-only-session-secret-change-me"  # noqa: S105 - overridden in any real deployment
    session_hours: int = 8
    approver_password: str = "change-me-approver"  # noqa: S105 - demo users, documented in .env.example
    admin_password: str = "change-me-admin"  # noqa: S105
    secure_cookies: bool = False
