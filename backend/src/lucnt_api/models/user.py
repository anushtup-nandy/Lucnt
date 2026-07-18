"""User ORM model, including the role enum used for RBAC (see CLAUDE.local.md)."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from lucnt_api.database import Base, CreatedAtMixin, UUIDPrimaryKeyMixin


class UserRole(str, enum.Enum):
    """Presets-now RBAC role, resolved through a single `resolve_perms(user)` seam.

    See `CLAUDE.local.md` "Design decision on RBAC": presets now, full
    table-backed permissions later, without a schema rewrite.
    """

    ENGINEER = "engineer"
    CFO = "cfo"
    AGENT_OWNER = "agent_owner"
    APPROVER = "approver"
    AUDITOR = "auditor"


class User(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """A human user belonging to a company."""

    __tablename__ = "users"

    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"))
    email: Mapped[str] = mapped_column(String(255), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole, name="user_role", values_callable=lambda e: [m.value for m in e])
    )
    password_hash: Mapped[str] = mapped_column(String(255))
    last_login: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    is_active: Mapped[bool] = mapped_column(default=True)
