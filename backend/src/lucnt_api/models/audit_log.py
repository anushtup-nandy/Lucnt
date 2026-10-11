"""AuditLog ORM model — append-only trail of state mutations.

A financial-controls product must record who changed what: budget edits, policy
create/deactivate, approvals, reconciliation resolutions. Each row is written
inside the mutation's own transaction, so the trail commits atomically with the
change it describes and can never be silently rewritten.
"""

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
    # Exactly one of the two actor columns is set, matching `AuthContext`'s own
    # invariant. Neither carries an FK: a revoked key or a deleted user's history
    # must survive them (an audit trail that a delete can erase is no trail).
    #
    # `actor_user_id` is the one column P7.0 adds (migration `c5d6e7f8a9b0`). The
    # phase was scoped "no migration", and this is the deliberate exception: with
    # the dashboard, humans mutate budgets, policies and approvals, and a
    # governance product that records those changes with a NULL actor is not
    # recording them. Additive and nullable, so every existing row stays valid.
    actor_api_key_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, default=None)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, default=None)
    action: Mapped[str] = mapped_column(String(100))
    entity_type: Mapped[str] = mapped_column(String(50))
    entity_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, default=None)
    # JSON-safe snapshots (money as strings) of the touched fields, before/after.
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)
