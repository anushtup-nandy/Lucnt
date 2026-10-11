"""Alembic environment: wires the app's SQLAlchemy metadata + DB URL into migrations."""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

import lucnt_api.models  # noqa: F401 -- populates Base.metadata for autogenerate
from lucnt_api.config import settings
from lucnt_api.database import Base

config = context.config

# configparser interpolates `%`, so a password containing one would break every
# read; escape it here. The engine below uses the raw URL.
config.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# Without these, autogenerate (and the `alembic check` drift gate) ignores type
# and server-default changes.
_COMPARE_OPTS = {"compare_type": True, "compare_server_default": True}


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode, emitting SQL without a live connection."""
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        **_COMPARE_OPTS,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode, against a live DB connection."""
    connectable = create_engine(settings.database_url, poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata, **_COMPARE_OPTS
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
