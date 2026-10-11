"""Workflow ORM model — a cross-agent budget rollup (Phase 3, M6).

A workflow groups several agents under one shared budget (e.g. "Q3 offsite":
the ops, travel, and research agents). Its *spent* figure is never stored — it
is derived by summing member agents' spend at read time (`routers/budgets.py`),
so it can never drift from the ledger. `workflow_agents` is the pure join table.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Numeric,
    String,
    Table,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from lucnt_api.database import Base, CreatedAtMixin, UUIDPrimaryKeyMixin
from lucnt_api.models.agent import Agent


class WorkflowStatus(enum.StrEnum):
    """Lifecycle of a workflow budget."""

    ACTIVE = "active"
    COMPLETED = "completed"
    ARCHIVED = "archived"


# Pure many-to-many join; no extra columns, so a Table (not a mapped class).
workflow_agents = Table(
    "workflow_agents",
    Base.metadata,
    Column("workflow_id", ForeignKey("workflows.id"), primary_key=True),
    Column("agent_id", ForeignKey("agents.id"), primary_key=True),
)


class Workflow(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """A named, budgeted grouping of agents whose spend rolls up together."""

    __tablename__ = "workflows"
    __table_args__ = (
        # Workflow budget rollups scope by company — `routers/budgets.py`.
        Index("ix_workflows_company_id", "company_id"),
    )

    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"))
    name: Mapped[str] = mapped_column(String(255))
    # NUMERIC, never FLOAT (Decimal, not float).
    budget: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=Decimal("0"))
    start_date: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    end_date: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    status: Mapped[WorkflowStatus] = mapped_column(
        Enum(
            WorkflowStatus,
            name="workflow_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        default=WorkflowStatus.ACTIVE,
    )
    agents: Mapped[list[Agent]] = relationship(secondary=workflow_agents)
