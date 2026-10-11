"""Memory ORM model: an agent's learned financial patterns (M8).

Recomputed from the ledger; `created_at` is "learned since".
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from lucnt_api.database import Base, CreatedAtMixin, UUIDPrimaryKeyMixin


class MemoryType(enum.StrEnum):
    """Learned-pattern kind: spend range/cadence, vendor affinity, cost/output."""

    SPEND_PATTERN = "spend_pattern"
    VENDOR_AFFINITY = "vendor_affinity"
    COST_PROFILE = "cost_profile"


class Memory(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """One learned financial pattern for an agent (the Memory page's rows)."""

    __tablename__ = "memories"
    __table_args__ = (
        # One per (agent, type); recompute upserts in place.
        UniqueConstraint("agent_id", "memory_type", name="uq_memories_agent_type"),
        # GET /memory scopes by company.
        Index("ix_memories_company_id", "company_id"),
    )

    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"))
    agent_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agents.id"))
    memory_type: Mapped[MemoryType] = mapped_column(
        Enum(
            MemoryType,
            name="memory_type",
            values_callable=lambda e: [m.value for m in e],
        )
    )
    summary: Mapped[str] = mapped_column(String(255))
    detail_text: Mapped[str] = mapped_column(Text)
    # Heterogeneous per memory type, hence `Any`.
    metrics_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
