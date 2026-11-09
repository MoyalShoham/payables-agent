"""Alembic environment: the database URL comes from PAYABLES_DATABASE_URL (or the settings default)."""

from __future__ import annotations

from alembic import context
from sqlalchemy import engine_from_config, pool, text

from payables.config import Settings
from payables.db import Base

config = context.config
config.set_main_option("sqlalchemy.url", Settings().database_url)
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"), target_metadata=target_metadata, literal_binds=True
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}), prefix="sqlalchemy.", poolclass=pool.NullPool
    )
    with connectable.connect() as connection:
        # Once the owner role exists, new objects belong to it, not to the bootstrap superuser.
        if connection.execute(text("SELECT 1 FROM pg_roles WHERE rolname = 'payables_owner'")).first():
            connection.execute(text("SET ROLE payables_owner"))
        connection.commit()
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
