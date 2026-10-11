"""Alembic environment: wires the app's SQLAlchemy metadata + DB URL into migrations."""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

import lucnt_api.models  # noqa: F401 -- populates Base.metadata for autogenerate
from lucnt_api.config import settings
from lucnt_api.database import Base

config = context.config

# `alembic.ini` is read by configparser with interpolation ON, so a `%` in
# the password (perfectly legal, and what a generated secret will eventually
# contain) makes every read of `sqlalchemy.url` raise `InterpolationSyntaxError`
# — i.e. migrations break on exactly the deployment where the credential is
# strong. The URL is still published for anything that inspects the config, but
# escaped; the engine below is built from the raw value and never round-trips
# through configparser at all.
config.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# Without these, Alembic's autogenerate diffs only table/column *existence*.
# A widened `String(120)`, a changed `Numeric` precision, or a new server default
# produces an empty migration and silently ships a schema that disagrees with the
# models — and `alembic check` (the CI drift gate) reports clean while it happens.
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
