"""Memory ORM model — an agent's learned financial patterns (Phase 5, M8).

A `memories` row is one durable, human-readable pattern the intelligence layer
has learned about an agent: its dominant vendor, its typical spend range, its
cost-per-output profile. Memories are recomputed from the ledger
(`POST /intelligence/recompute`), never written on the capture path, so the
frozen `authorize→capture` spine stays lane-agnostic (`CLAUDE.local.md`).

At most one memory of each `memory_type` exists per agent
(`uq_memories_agent_type`); recompute upserts it, so `created_at` records when
the pattern was first learned ("learned since") and `updated_at` when it was last
refreshed — the accumulating, company-specific memory that is M8's moat.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone
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


class MemoryType(str, enum.Enum):
    """The kind of learned pattern a memory records.

    `SPEND_PATTERN` — the agent's typical amount range and cadence.
    `VENDOR_AFFINITY` — the vendor(s) it concentrates spend on.
    `COST_PROFILE` — its cost per unit of output (per task type / project).
    """

    SPEND_PATTERN = "spend_pattern"
    VENDOR_AFFINITY = "vendor_affinity"
    COST_PROFILE = "cost_profile"


class Memory(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """One learned financial pattern for an agent (the Memory page's rows)."""

    __tablename__ = "memories"
    __table_args__ = (
        # One memory of each type per agent — recompute upserts in place so a
        # learned pattern is a single evolving record, not a growing log.
        UniqueConstraint("agent_id", "memory_type", name="uq_memories_agent_type"),
        # GET /memory scopes by company (T3-1); agent_id lookups already ride
        # uq_memories_agent_type's leading column.
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
    # Structured metrics backing the summary (amounts, counts, shares). `Any` is
    # justified as elsewhere: heterogeneous JSON per memory type.
    metrics_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
