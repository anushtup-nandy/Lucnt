"""Shared pytest fixtures: a transactional DB session against the migrated schema.

Requires `alembic upgrade head` to have been run against the target database
first -- these are integration tests against the real migrated schema, not a
SQLAlchemy `create_all()` shortcut.

**Which database.** Set `TEST_DATABASE_URL` and the suite runs against
it; leave it unset and it falls back to `DATABASE_URL`, i.e. the developer's own
database, with a warning. The fallback is safe for the suite itself (every
fixture rolls its transaction back) but not for CI's migration round-trip, which
runs `alembic downgrade base` against whatever it is pointed at.
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
    """Skip the lifespan readiness check for the suite.

    `TestClient(app)` runs the real lifespan, whose check pings the module-level
    engine in `database.py`. Tests never use that engine -- routes are served
    sessions through `dependency_overrides` -- so the check would be asserting
    about a database the suite does not touch, and would fail the entire suite
    whenever `DATABASE_URL` happens to be down. The check itself is covered
    directly in `tests/integration/test_startup_and_health.py`.
    """
    original = settings.startup_checks_enabled
    settings.startup_checks_enabled = False
    try:
        yield
    finally:
        settings.startup_checks_enabled = original


@pytest.fixture(autouse=True)
def _rate_limiting_off() -> Iterator[None]:
    """Disable rate limiting for the suite, and say why explicitly.

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
