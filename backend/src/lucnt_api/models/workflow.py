"""Workflow ORM model: agents grouped under one shared budget (M6).

Spend is never stored; it's derived from members at read time so it can't drift.
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
        # Budget rollups scope by company.
        Index("ix_workflows_company_id", "company_id"),
    )

    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"))
    name: Mapped[str] = mapped_column(String(255))
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
