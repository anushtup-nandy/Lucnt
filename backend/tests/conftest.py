"""Shared pytest fixtures: a transactional DB session against the migrated schema.

Requires `alembic upgrade head` to have been run against `DATABASE_URL` first
-- these are integration tests against the real migrated schema, not a
SQLAlchemy `create_all()` shortcut.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy.orm import Session

from lucnt.database import SessionLocal, engine


@pytest.fixture
def db_session() -> Iterator[Session]:
    """A DB session bound to a transaction that is rolled back after the test."""
    connection = engine.connect()
    transaction = connection.begin()
    session = SessionLocal(bind=connection)
    try:
        yield session
    finally:
        session.close()
        if transaction.is_active:
            transaction.rollback()
        connection.close()
