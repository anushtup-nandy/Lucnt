"""Insight ORM model — the Intelligence layer's recommendations (Phase 5, M8).

An `insights` row is a derived, actionable recommendation the intelligence layer
produces from the ledger: an agent projected to overrun its budget, an anomalous
spend, or an expensive unit of output ("your CFO, always on"). Insights are
recomputed from the ledger (`POST /intelligence/recompute`), never written on the
capture path, so the frozen `authorize→capture` spine stays lane-agnostic
(`CLAUDE.local.md`) — like the Phase 4 recovery scan.

Recompute clears the company's **un-applied** insights and regenerates them (they
are ephemeral, derived state); an *applied* insight persists as a record of the
action a human took (`is_applied` / `applied_at`).
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
    """The kind of recommendation an insight carries.

    All three are honestly computable from ledger data alone:
    `BUDGET_RISK` — an agent's run-rate projects past its monthly budget.
    `ANOMALY` — an agent's recent spend is a statistical outlier vs its own
    fingerprint (`fingerprints.anomaly_score` at/above threshold).
    `COST_SPIKE` — a unit of output (task type / project) costs far more than
    the company's typical unit.
    """

    BUDGET_RISK = "budget_risk"
    ANOMALY = "anomaly"
    COST_SPIKE = "cost_spike"


class Insight(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """One actionable recommendation from the intelligence layer (the Insights page)."""

    __tablename__ = "insights"
    __table_args__ = (
        # The un-dismissed insight feed (T3-1) — `routers/insights.py:64-70`.
        Index("ix_insights_company_id_is_dismissed", "company_id", "is_dismissed"),
    )

    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"))
    # The agent an insight is about; null for company-level insights.
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
    # Stable natural key (type:agent:subject) so recompute upserts an insight in
    # place instead of delete+insert (stable ids; T2-10). Nullable for rows that
    # predate the column.
    dedup_key: Mapped[str | None] = mapped_column(Text, default=None)
    title: Mapped[str] = mapped_column(String(255))
    body: Mapped[str] = mapped_column(Text)
    # A headline figure for the card (e.g. "$1,240 projected overrun"); frontend
    # renders it prominently. Null when the insight has no single number.
    value_text: Mapped[str | None] = mapped_column(String(100), default=None)
    # The one-click action offered (e.g. "Raise budget"), with a structured
    # payload the dashboard/SDK can act on. `Any` is justified as elsewhere.
    action_label: Mapped[str | None] = mapped_column(String(100), default=None)
    action_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    is_applied: Mapped[bool] = mapped_column(Boolean, default=False)
    applied_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    # A reviewed-but-not-applied insight can be dismissed; a dismissed insight is
    # suppressed from regeneration so it does not nag forever (T2-10).
    is_dismissed: Mapped[bool] = mapped_column(Boolean, default=False)
    dismissed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
