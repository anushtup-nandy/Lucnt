"""Pydantic schemas for the Intelligence layer (Phase 5, M8).

Covers the Insights and Memory pages (`routers/insights.py`, `routers/memory.py`),
the recompute pass and the living financial-memory read (`routers/intelligence.py`,
the SDK `query()` backing). Enum columns surface as plain `str` (routers map
`.value`), mirroring `schemas/recon.py`; heterogeneous JSON is typed `Any` as
justified elsewhere.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict

# --- Insights (GET /insights, POST /insights/{id}/apply) ---


class InsightResponse(BaseModel):
    """One actionable recommendation from the intelligence layer."""

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    agent_id: uuid.UUID | None
    insight_type: str
    title: str
    body: str
    value_text: str | None
    action_label: str | None
    action_payload: dict[str, Any]
    is_applied: bool
    applied_at: datetime | None
    is_dismissed: bool
    dismissed_at: datetime | None
    created_at: datetime


class InsightsListResponse(BaseModel):
    """Response for `GET /insights` — the Insights page plus an unapplied count."""

    model_config = ConfigDict(frozen=True)

    items: list[InsightResponse]
    total: int
    unapplied_count: int


# --- Memory (GET /memory) ---


class MemoryResponse(BaseModel):
    """One learned financial pattern for an agent."""

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    agent_id: uuid.UUID
    memory_type: str
    summary: str
    detail_text: str
    metrics_json: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class MemoryListResponse(BaseModel):
    """Response for `GET /memory` — learned patterns across the company's agents."""

    model_config = ConfigDict(frozen=True)

    items: list[MemoryResponse]
    total: int


# --- Recompute (POST /intelligence/recompute) ---


class RecomputeResponse(BaseModel):
    """Summary of an intelligence recompute pass."""

    model_config = ConfigDict(frozen=True)

    agents_analyzed: int
    fingerprints: int
    memories: int
    insights: int
    by_insight_type: dict[str, int]


# --- Living financial memory (GET /intelligence/query — the SDK query()) ---


class BudgetBlock(BaseModel):
    """Live budget headroom (mirrors `GET /budgets` / `check_budget()`)."""

    model_config = ConfigDict(frozen=True)

    monthly_budget: Decimal
    spent: Decimal
    remaining: Decimal
    pct_used: Decimal
    status: str  # ok | warning | exhausted


class ProjectionBlock(BaseModel):
    """Month-end spend projection from the current run-rate."""

    model_config = ConfigDict(frozen=True)

    month_to_date: Decimal
    projected_month_end: Decimal
    monthly_budget: Decimal
    on_track: bool
    projected_overrun: Decimal
    day_of_month: int
    days_in_month: int


class CostUnitBlock(BaseModel):
    """Cost-per-output for one unit of work (a project), in one currency."""

    model_config = ConfigDict(frozen=True)

    unit: str
    dimension: str
    currency: str
    total_spent: Decimal
    count: int
    avg_cost: Decimal
    total_tokens: int | None
    cost_per_1k_tokens: Decimal | None


class VendorShareBlock(BaseModel):
    """One vendor's share of an agent's transaction count."""

    model_config = ConfigDict(frozen=True)

    vendor: str
    count: int
    share: Decimal


class FingerprintBlock(BaseModel):
    """A compact view of an agent's spend fingerprint (null stats until learned)."""

    model_config = ConfigDict(frozen=True)

    home_currency: str | None
    total_records: int
    sample_size: int
    mean_amount: Decimal | None
    stddev_amount: Decimal | None
    median_amount: Decimal | None
    daily_velocity: Decimal
    anomaly_score: Decimal
    top_vendors: list[VendorShareBlock]


class FinancialMemoryResponse(BaseModel):
    """Living financial memory for one agent — the SDK `query()` response.

    Computed live from the agent's ledger so an agent can self-regulate *before*
    it spends (M8). `headline` answers the free-text `q`, keyword-routed to the
    most relevant dimension (budget / cost / on-track) — no LLM involved.
    """

    model_config = ConfigDict(frozen=True)

    agent: str
    currency: str
    headline: str
    budget: BudgetBlock
    projection: ProjectionBlock
    cost_per_output: list[CostUnitBlock]
    fingerprint: FingerprintBlock | None
