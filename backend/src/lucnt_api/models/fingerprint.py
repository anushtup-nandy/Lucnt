"""Fingerprint ORM model: an agent's learned spend profile (M8).

Recomputed from the ledger, never on the capture path.
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
        # One per agent; recompute upserts in place.
        UniqueConstraint("agent_id", name="uq_fingerprints_agent"),
        # Company-scoped reads during recompute; agent_id is unique already.
        Index("ix_fingerprints_company_id", "company_id"),
    )

    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"))
    agent_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agents.id"))
    # Heterogeneous JSON from services/intelligence.py, hence `Any`.
    feature_vector: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    # Max z-score of recent spends vs the agent's own baseline.
    anomaly_score: Mapped[Decimal] = mapped_column(Numeric(10, 4), default=Decimal("0"))
    last_updated: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
