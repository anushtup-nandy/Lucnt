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

# Explicitly configured pool + timeouts (T3-4). Every value is sourced from
# Settings, never hardcoded here — see `config.py` for the sizing rationale.
#
# `pool_pre_ping` costs one cheap round-trip per checkout and removes the entire
# class of "first request after an idle period 500s" failures (a connection
# reaped by Postgres, a proxy, or a container restart is detected and replaced
# instead of handed to a request). `statement_timeout` is set as a server-side
# session option so a runaway query is killed by Postgres rather than holding a
# pool connection for as long as the client is willing to wait.
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
    """UUIDv7 primary key — sortable by creation time (backend/PLAN.md §3)."""

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)


class CreatedAtMixin:
    """`created_at` audit column, present on every table (backend/PLAN.md §3)."""

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
