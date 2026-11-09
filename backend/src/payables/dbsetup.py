"""After migrations, as the bootstrap superuser: python -m payables.dbsetup

- lets the two application roles log in, with passwords from secret files;
- creates the demo users (agent, approver, admin) if they do not exist.

The migrations create the roles without a login; passwords never appear in a migration.
"""

from __future__ import annotations

from pathlib import Path

from psycopg import sql
from pydantic_settings import BaseSettings, SettingsConfigDict

from payables.config import Settings, _check_secret, _read
from payables.db import AGENT_ROLE, API_ROLE, make_engine, make_sessionmaker
from payables.extraction.recording import RecordingStore, ReplayExtractor
from payables.services import Payables


class SetupSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PAYABLES_", env_file=".env", extra="ignore")

    api_db_password_file: Path | None = None
    agent_db_password_file: Path | None = None
    approver_password: str = "change-me-approver"  # noqa: S105 - documented demo user; set in .env
    admin_password: str = "change-me-admin"  # noqa: S105
    approver_email: str = "approver@payables.example"
    admin_email: str = "admin@payables.example"


def set_role_passwords(database_url: str, passwords: dict[str, str]) -> None:
    engine = make_engine(database_url)
    try:
        with engine.begin() as conn:
            for role, password in passwords.items():
                raw = conn.connection.driver_connection
                assert raw is not None
                with raw.cursor() as cur:
                    cur.execute(
                        sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD {}").format(
                            sql.Identifier(role), sql.Literal(password)
                        )
                    )
    finally:
        engine.dispose()


def main() -> None:
    settings = Settings()
    setup = SetupSettings()
    passwords = {}
    for role, path in ((API_ROLE, setup.api_db_password_file), (AGENT_ROLE, setup.agent_db_password_file)):
        if path is not None:
            passwords[role] = _check_secret(f"password for {role}", _read(path))
    set_role_passwords(settings.database_url, passwords)
    service = Payables(
        make_sessionmaker(make_engine(settings.database_url)),
        ReplayExtractor(RecordingStore(settings.recordings_dir)),
        audit_key=b"",  # creating users writes no audit entries
    )
    service.ensure_users(
        setup.approver_password,
        setup.admin_password,
        {"approver": setup.approver_email, "admin": setup.admin_email},
    )
    print(f"login enabled for {', '.join(passwords) or 'no roles'}; demo users present")


if __name__ == "__main__":
    main()
