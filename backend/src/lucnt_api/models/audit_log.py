"""AuditLog ORM model: append-only trail, written atomically with each mutation."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import ForeignKey, Index, String, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from lucnt_api.database import Base, CreatedAtMixin, UUIDPrimaryKeyMixin


class AuditLog(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """One append-only record of a governance mutation: who did what, before→after."""

    __tablename__ = "audit_logs"
    __table_args__ = (Index("ix_audit_logs_company_id", "company_id"),)

    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"))
    # Exactly one actor is set. No FKs: history must survive a revoked key or
    # deleted user.
    actor_api_key_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, default=None)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, default=None)
    action: Mapped[str] = mapped_column(String(100))
    entity_type: Mapped[str] = mapped_column(String(50))
    entity_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, default=None)
    # JSON-safe snapshots (money as strings) of the touched fields.
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)
