"""Policy ORM model — one governance rule evaluated at `authorize` (Phase 3, M5).

A policy is the unit the policy engine (`services/policy_engine.py`) evaluates
*before* money moves, so it can block a spend rather than merely record it. Each
rule carries a typed config in `config_json` (JSONB); the engine parses it into a
discriminated Pydantic union per `rule_type`. Policies are layered company→agent
(`scope_type`/`scope_id`) per `CLAUDE.local.md` (M5).
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, Enum, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from lucnt_api.database import Base, CreatedAtMixin, UUIDPrimaryKeyMixin


class PolicyRuleType(enum.StrEnum):
    """The kind of rule a policy encodes; selects the config schema + evaluator."""

    BUDGET_CAP = "budget_cap"
    MERCHANT_LIST = "merchant_list"
    VELOCITY = "velocity"
    AMOUNT_THRESHOLD = "amount_threshold"
    TIME_OF_DAY = "time_of_day"


class PolicyScopeType(enum.StrEnum):
    """Whether a policy applies company-wide or to a single agent.

    `company` policies (`scope_id is None`) form the base layer; `agent`
    policies (`scope_id == agents.id`) layer on top for that agent only.
    """

    COMPANY = "company"
    AGENT = "agent"


class Policy(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """A single governance rule the policy engine evaluates at authorize time."""

    __tablename__ = "policies"
    __table_args__ = (
        # Read on every authorize() (`routers/transactions.py:129-131`) — an
        # unindexed sequential scan of the whole policy table per call (T3-1).
        Index("ix_policies_company_id_active", "company_id", "active"),
    )

    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"))
    name: Mapped[str] = mapped_column(String(255))
    rule_type: Mapped[PolicyRuleType] = mapped_column(
        Enum(
            PolicyRuleType,
            name="policy_rule_type",
            values_callable=lambda e: [m.value for m in e],
        )
    )
    # Rule-specific config, shape determined by `rule_type`. `Any` is justified:
    # the value is genuinely heterogeneous JSON; it is parsed into a typed,
    # discriminated Pydantic model at evaluation time (`services/policy_engine.py`).
    config_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    scope_type: Mapped[PolicyScopeType] = mapped_column(
        Enum(
            PolicyScopeType,
            name="policy_scope_type",
            values_callable=lambda e: [m.value for m in e],
        ),
        default=PolicyScopeType.COMPANY,
    )
    # Null for a company-wide policy; the target `agents.id` for an agent-scoped
    # one. Beyond root PLAN.md §3's sketch, but required for the layered
    # company→agent scoping M5 mandates (CLAUDE.local.md). No FK: an agent-scoped
    # policy referencing a since-deleted agent should simply stop matching, not
    # block deletion — the engine already filters by live agent id.
    scope_id: Mapped[uuid.UUID | None] = mapped_column(default=None)
    active: Mapped[bool] = mapped_column(default=True)
    # The engine's explanation when this rule decides a verdict (stored on the
    # authorization's `reason_text`); optional, falls back to a generated string.
    description: Mapped[str | None] = mapped_column(Text, default=None)
    hit_count: Mapped[int] = mapped_column(default=0)
    block_count: Mapped[int] = mapped_column(default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
