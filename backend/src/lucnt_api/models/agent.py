"""Agent ORM model — an autonomous spender attached to an entity (PLAN.md §3)."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import DateTime, Enum, ForeignKey, Numeric, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from lucnt_api.database import Base, CreatedAtMixin, UUIDPrimaryKeyMixin


class AgentStatus(str, enum.Enum):
    """Lifecycle status of an agent.

    Distinct from mockup display labels like "Budget exhausted", which are
    derived at read time from spend/budget, not stored.
    """

    ACTIVE = "active"
    PAUSED = "paused"
    DISABLED = "disabled"


class Agent(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """An autonomous agent whose spend is governed via authorize()/capture()."""

    __tablename__ = "agents"
    __table_args__ = (
        UniqueConstraint("company_id", "slug", name="uq_agents_company_slug"),
    )

    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"))
    entity_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("entities.id"))
    name: Mapped[str] = mapped_column(String(255))
    slug: Mapped[str] = mapped_column(String(100))
    glyph: Mapped[str] = mapped_column(String(10))
    role: Mapped[str] = mapped_column(String(255))
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id"), default=None
    )
    wallet_ref: Mapped[str | None] = mapped_column(String(100), default=None)
    models_list: Mapped[list[str]] = mapped_column(JSONB, default=list)
    vendors_list: Mapped[list[str]] = mapped_column(JSONB, default=list)
    # NUMERIC, never FLOAT -- money/ledger fields (sdk/PLAN.md: Decimal not float).
    monthly_budget: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), default=Decimal("0")
    )
    spent_ytd: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=Decimal("0"))
    spent_today: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=Decimal("0"))
    status: Mapped[AgentStatus] = mapped_column(
        Enum(
            AgentStatus,
            name="agent_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        default=AgentStatus.ACTIVE,
    )
    is_live: Mapped[bool] = mapped_column(default=True)
    confidence_pct: Mapped[int] = mapped_column(default=100)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
