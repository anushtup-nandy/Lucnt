"""Shared pytest fixtures: a transactional DB session against the migrated schema.

Requires `alembic upgrade head` to have been run against `DATABASE_URL` first
-- these are integration tests against the real migrated schema, not a
SQLAlchemy `create_all()` shortcut.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy.orm import Session, sessionmaker

from lucnt.database import engine


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
