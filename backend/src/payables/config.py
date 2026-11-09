"""Settings from the environment (and a .env file in development).

Secrets can be given directly (PAYABLES_SESSION_SECRET) or as a file (PAYABLES_SESSION_SECRET_FILE),
which is how Docker Compose passes them: an init container writes random secrets into a volume on
first start. The API refuses to start with a missing, short or well-known session secret, and every
process that writes to the audit log needs the audit key.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

MIN_SECRET_BYTES = 32
#: Placeholders that have appeared in this repository's docs or defaults. Never accepted.
KNOWN_DEFAULTS = frozenset(
    {
        "dev-only-session-secret-change-me",
        "replace-with-a-long-random-string",
        "change-me",
        "changeme",
        "secret",
        "test-secret",
    }
)


class ExtractionMode(StrEnum):
    #: Call Claude for every new document.
    LIVE = "live"
    #: Call Claude once per document and keep the answer (development).
    RECORD = "record"
    #: Only stored answers; no API key needed (tests, CI, the offline demo).
    REPLAY = "replay"


class InsecureConfigError(RuntimeError):
    """A secret is missing or unsafe; the process refuses to start."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PAYABLES_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://payables_api@localhost:5432/payables"
    #: The database password, if not in the URL (Compose passes it as a file).
    database_password_file: Path | None = None
    extraction: ExtractionMode = ExtractionMode.REPLAY
    recordings_dir: Path = Path(__file__).resolve().parents[2] / "eval" / "recordings"
    session_secret: str | None = None
    session_secret_file: Path | None = None
    #: HMAC key for the audit chain. Kept outside the database, so database access alone cannot
    #: rewrite history and recompute the hashes.
    audit_key: str | None = None
    audit_key_file: Path | None = None
    #: Append-only file of audit checkpoints (head hash and count), outside the database.
    audit_anchors: Path | None = None
    session_hours: int = 8
    secure_cookies: bool = False
    #: Serve the OpenAPI docs at /api/docs (off by default).
    api_docs: bool = False
    #: Pins "today" for the date checks. Only for seeding the demo, whose invoices are dated mid-2025.
    today: date | None = None

    @model_validator(mode="after")
    def _read_secret_files(self) -> Settings:
        if self.session_secret is None and self.session_secret_file is not None:
            self.session_secret = _read(self.session_secret_file)
        if self.audit_key is None and self.audit_key_file is not None:
            self.audit_key = _read(self.audit_key_file)
        if self.database_password_file is not None:
            url = make_url(self.database_url).set(password=_read(self.database_password_file))
            self.database_url = url.render_as_string(hide_password=False)
        return self

    def require_session_secret(self) -> str:
        return _check_secret("PAYABLES_SESSION_SECRET", self.session_secret)

    def require_audit_key(self) -> bytes:
        return _check_secret("PAYABLES_AUDIT_KEY", self.audit_key).encode()


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError as e:
        raise InsecureConfigError(f"cannot read the secret file {path}: {e.strerror}") from e


def _check_secret(name: str, value: str | None) -> str:
    if not value:
        raise InsecureConfigError(f"{name} (or {name}_FILE) is not set; refusing to start")
    if value.strip().lower() in KNOWN_DEFAULTS:
        raise InsecureConfigError(f"{name} is a published placeholder; set a random value")
    if len(value.encode()) < MIN_SECRET_BYTES:
        raise InsecureConfigError(f"{name} must be at least {MIN_SECRET_BYTES} bytes")
    return value
