"""Insight ORM model: derived recommendations from the intelligence layer (M8).

Recompute regenerates un-applied insights; applied ones persist as an action record.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from lucnt_api.database import Base, CreatedAtMixin, UUIDPrimaryKeyMixin


class InsightType(enum.StrEnum):
    """Recommendation kind, computable from ledger data alone.

    `BUDGET_RISK`: run-rate projects past monthly budget. `ANOMALY`: outlier vs the
    agent's fingerprint. `COST_SPIKE`: a unit of output costs far above typical.
    """

    BUDGET_RISK = "budget_risk"
    ANOMALY = "anomaly"
    COST_SPIKE = "cost_spike"


class Insight(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """One actionable recommendation from the intelligence layer (the Insights page)."""

    __tablename__ = "insights"
    __table_args__ = (
        # The un-dismissed insight feed.
        Index("ix_insights_company_id_is_dismissed", "company_id", "is_dismissed"),
    )

    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"))
    # Null for company-level insights.
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agents.id"), default=None
    )
    insight_type: Mapped[InsightType] = mapped_column(
        Enum(
            InsightType,
            name="insight_type",
            values_callable=lambda e: [m.value for m in e],
        )
    )
    # Natural key (type:agent:subject) so recompute upserts with stable ids.
    dedup_key: Mapped[str | None] = mapped_column(Text, default=None)
    title: Mapped[str] = mapped_column(String(255))
    body: Mapped[str] = mapped_column(Text)
    # Headline figure for the card; null when there is no single number.
    value_text: Mapped[str | None] = mapped_column(String(100), default=None)
    # One-click action + structured payload (heterogeneous JSON, hence `Any`).
    action_label: Mapped[str | None] = mapped_column(String(100), default=None)
    action_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    is_applied: Mapped[bool] = mapped_column(Boolean, default=False)
    applied_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    # Dismissed insights are suppressed from regeneration so they don't nag.
    is_dismissed: Mapped[bool] = mapped_column(Boolean, default=False)
    dismissed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
