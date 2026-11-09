"""Creates the deployment's secrets on first start: python -m payables.secretfiles init /run/payables-secrets

Writes one random value per file and never overwrites an existing file, so restarts keep the same
secrets. Docker Compose runs this in an init container before anything else starts.
"""

from __future__ import annotations

import argparse
import os
import secrets
from pathlib import Path

#: file name -> what it is
SECRETS = {
    "session_secret": "signs session cookies (API)",
    "audit_key": "HMAC key of the audit chain (API, agent, seed)",
    "db_bootstrap_password": "PostgreSQL superuser, used only by migrations and the demo seed",
    "db_api_password": "database role payables_api",
    "db_agent_password": "database role payables_agent",
}


def init(directory: Path) -> list[str]:
    directory.mkdir(parents=True, exist_ok=True)
    created = []
    for name in SECRETS:
        path = directory / name
        if path.exists():
            continue
        # O_EXCL: two init containers racing cannot both write the same file.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(secrets.token_urlsafe(48))
        created.append(name)
    return created


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m payables.secretfiles")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("init")
    p.add_argument("directory", type=Path)
    args = parser.parse_args(argv)
    created = init(args.directory)
    print(f"created {', '.join(created)}" if created else "secrets already present")


if __name__ == "__main__":
    main()
