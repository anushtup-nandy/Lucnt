"""Shared pytest fixtures: a transactional DB session against the migrated schema.

Requires `alembic upgrade head` to have been run against `DATABASE_URL` first
-- these are integration tests against the real migrated schema, not a
SQLAlchemy `create_all()` shortcut.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy.orm import Session, sessionmaker

from lucnt_api.config import settings
from lucnt_api.database import engine
from lucnt_api.rate_limit import get_limiter


@pytest.fixture(autouse=True)
def _rate_limiting_off() -> Iterator[None]:
    """Disable rate limiting for the suite, and say why explicitly (T3-7).

    Redis runs in local compose, so leaving the limiter on would make the suite
    depend on wall-clock windows and on counters surviving between runs: the
    `auth_failure` bucket is keyed by IP, every test shares `testclient`, and
    several tests deliberately send bad keys. Tests that *exercise* the limiter
    re-enable it and use a unique identity per test so their window starts empty.
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
    """A DB session bound to a transaction that is rolled back after the test.

    `join_transaction_mode="create_savepoint"` lets route/dependency code call
    `session.commit()` (as `routers/transactions.py` and `dependencies.py` do)
    without ending the outer transaction early -- each commit becomes a
    SAVEPOINT release instead, so the final rollback below still discards
    everything the test wrote (SQLAlchemy's external-transaction test recipe).
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
