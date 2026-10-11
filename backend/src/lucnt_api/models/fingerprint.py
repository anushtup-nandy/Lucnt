"""Fingerprint ORM model — an agent's learned spend fingerprint (Phase 5, M8).

A `fingerprints` row is one agent's `feature_vector` — a compact statistical
summary of how it spends (mean/stddev amount, top vendors, active hours, daily
velocity) plus an `anomaly_score` for its most out-of-pattern recent spend. The
intelligence layer recomputes it from the ledger (`POST /intelligence/recompute`)
rather than on the capture path, so the frozen `authorize→capture` spine stays
lane-agnostic — exactly like the Phase 4 recovery scan.

Exactly one fingerprint exists per agent (`uq_fingerprints_agent`); recompute
upserts it in place, so `created_at` records when the agent was first
fingerprinted and `last_updated` when it was last refreshed. The fingerprint is
the heart of the switching cost: it accrues value with every transaction and
resets to zero on any competitor.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, Numeric, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from lucnt_api.database import Base, CreatedAtMixin, UUIDPrimaryKeyMixin


class Fingerprint(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """One agent's spend fingerprint (feature vector + anomaly score)."""

    __tablename__ = "fingerprints"
    __table_args__ = (
        # One fingerprint per agent, ever — recompute upserts this row rather
        # than appending, so the fingerprint is a single evolving record.
        UniqueConstraint("agent_id", name="uq_fingerprints_agent"),
        # Company-scoped reads during recompute; agent_id is unique already.
        Index("ix_fingerprints_company_id", "company_id"),
    )

    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"))
    agent_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agents.id"))
    # The computed statistical summary. `Any` is justified (as in
    # Policy.config_json / ReconciliationException.evidence_json): a
    # heterogeneous JSON document produced/consumed by the pure intelligence
    # service (`services/intelligence.py`).
    feature_vector: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    # Max z-score among the agent's recent spends vs its own baseline — a rolling
    # "how out of pattern is it lately" health signal. NUMERIC, never FLOAT
    # (Decimal, not float).
    anomaly_score: Mapped[Decimal] = mapped_column(Numeric(10, 4), default=Decimal("0"))
    last_updated: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
