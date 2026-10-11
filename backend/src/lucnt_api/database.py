"""SQLAlchemy engine, session factory, declarative base, and shared mixins."""

from __future__ import annotations

import uuid
from collections.abc import Generator
from datetime import UTC, datetime

from sqlalchemy import DateTime, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from sqlalchemy.types import Uuid
from uuid6 import uuid7

from lucnt_api.config import settings

# pre_ping replaces connections reaped while idle; statement_timeout is
# server-side so Postgres kills runaway queries. Sizing: see `config.py`.
engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow,
    pool_timeout=settings.db_pool_timeout,
    pool_recycle=settings.db_pool_recycle,
    connect_args={
        "connect_timeout": settings.db_connect_timeout,
        "options": f"-c statement_timeout={settings.db_statement_timeout_ms}",
    },
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


class UUIDPrimaryKeyMixin:
    """UUIDv7 primary key — sortable by creation time."""

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)


class CreatedAtMixin:
    """`created_at` audit column, present on every table."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency yielding a request-scoped DB session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
