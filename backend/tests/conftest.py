"""Shared fixtures: rolled-back DB sessions against the migrated schema.

Requires `alembic upgrade head` first. Uses `TEST_DATABASE_URL`, else falls back
to `DATABASE_URL` (safe for tests, not for a migration downgrade round-trip).
"""

from __future__ import annotations

import warnings
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from lucnt_api.config import settings
from lucnt_api.database import engine as default_engine
from lucnt_api.rate_limit import get_limiter


def _resolve_engine() -> Engine:
    """The dedicated test engine if one is configured, else the app's."""
    if settings.test_database_url is None:
        warnings.warn(
            "TEST_DATABASE_URL is unset: running against DATABASE_URL. Fixtures "
            "roll back, but any destructive step (alembic downgrade) will hit "
            "your development database.",
            stacklevel=2,
        )
        return default_engine
    return create_engine(settings.test_database_url, pool_pre_ping=True)


engine = _resolve_engine()


@pytest.fixture(scope="session")
def resolved_engine() -> Engine:
    """The engine the suite is actually bound to (`tests/` is not importable)."""
    return engine


@pytest.fixture(autouse=True, scope="session")
def _startup_checks_off() -> Iterator[None]:
    """Skip the lifespan DB check: it pings an engine the suite never uses."""
    original = settings.startup_checks_enabled
    settings.startup_checks_enabled = False
    try:
        yield
    finally:
        settings.startup_checks_enabled = original


@pytest.fixture(autouse=True)
def _rate_limiting_off() -> Iterator[None]:
    """Disable rate limiting: buckets would persist in Redis across tests/runs.

    Limiter tests re-enable it with a unique identity so their window starts empty.
    """
    original = settings.rate_limit_enabled
    settings.rate_limit_enabled = False
    get_limiter.cache_clear()
    try:
        yield
    finally:
        settings.rate_limit_enabled = original
        get_limiter.cache_clear()


@pytest.fixture
def db_session() -> Iterator[Session]:
    """A DB session whose outer transaction is rolled back after the test.

    `create_savepoint` turns app-side `commit()` into a SAVEPOINT release.
    """
    connection = engine.connect()
    transaction = connection.begin()
    session_factory = sessionmaker(
        bind=connection, join_transaction_mode="create_savepoint"
    )
    session = session_factory()
    try:
        yield session
    finally:
        session.close()
        if transaction.is_active:
            transaction.rollback()
        connection.close()
