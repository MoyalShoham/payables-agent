"""Startup refuses unsafe secrets; secret files are created once and never overwritten."""

from __future__ import annotations

from pathlib import Path

import pytest

from payables.config import InsecureConfigError, Settings
from payables.ingest import html_to_text
from payables.secretfiles import SECRETS, init


@pytest.mark.parametrize(
    "secret",
    [None, "", "dev-only-session-secret-change-me", "replace-with-a-long-random-string", "short"],
)
def test_the_api_refuses_to_start_with_a_missing_default_or_short_secret(secret: str | None) -> None:
    from payables.api import create_app
    from payables.services import Payables

    settings = Settings(session_secret=secret)
    with pytest.raises(InsecureConfigError):
        create_app(Payables(None, None, b""), settings)  # type: ignore[arg-type]


def test_secrets_and_the_database_password_can_come_from_files(tmp_path: Path) -> None:
    (tmp_path / "s").write_text("x" * 40 + "\n")
    (tmp_path / "k").write_text("y" * 40)
    (tmp_path / "db").write_text("p@ss/word")
    settings = Settings(
        session_secret_file=tmp_path / "s",
        audit_key_file=tmp_path / "k",
        database_url="postgresql+psycopg://payables_api@db:5432/payables",
        database_password_file=tmp_path / "db",
    )
    assert settings.require_session_secret() == "x" * 40
    assert settings.require_audit_key() == b"y" * 40
    assert "payables_api:p%40ss%2Fword@db" in settings.database_url
    with pytest.raises(InsecureConfigError, match="cannot read"):
        Settings(session_secret_file=tmp_path / "missing")
    with pytest.raises(InsecureConfigError, match="PAYABLES_AUDIT_KEY"):
        Settings().require_audit_key()


def test_secret_files_are_created_once(tmp_path: Path) -> None:
    created = init(tmp_path / "secrets")
    assert created == list(SECRETS)
    first = {name: (tmp_path / "secrets" / name).read_text() for name in SECRETS}
    assert all(len(v) >= 48 for v in first.values())
    assert len(set(first.values())) == len(first)
    assert init(tmp_path / "secrets") == []
    assert {name: (tmp_path / "secrets" / name).read_text() for name in SECRETS} == first


def test_html_email_bodies_become_text() -> None:
    markup = (
        "<style>p{}</style><p>Hello&nbsp;there,</p><div>our <b>account</b> has changed</div>"
        "<script>x</script>"
    )
    assert html_to_text(markup) == "Hello there,\nour account has changed"
