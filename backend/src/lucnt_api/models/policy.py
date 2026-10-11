"""Policy ORM model: one governance rule evaluated at authorize (M5)."""

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
    """`company` policies (no scope_id) are the base; `agent` ones layer on top."""

    COMPANY = "company"
    AGENT = "agent"


class Policy(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """A single governance rule the policy engine evaluates at authorize time."""

    __tablename__ = "policies"
    __table_args__ = (
        # Read on every authorize().
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
    # Shape set by `rule_type`; parsed into a typed Pydantic union at evaluation.
    config_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    scope_type: Mapped[PolicyScopeType] = mapped_column(
        Enum(
            PolicyScopeType,
            name="policy_scope_type",
            values_callable=lambda e: [m.value for m in e],
        ),
        default=PolicyScopeType.COMPANY,
    )
    # Null = company-wide. No FK: a policy for a deleted agent just stops matching
    # rather than blocking deletion.
    scope_id: Mapped[uuid.UUID | None] = mapped_column(default=None)
    active: Mapped[bool] = mapped_column(default=True)
    # Verdict explanation; falls back to a generated string.
    description: Mapped[str | None] = mapped_column(Text, default=None)
    hit_count: Mapped[int] = mapped_column(default=0)
    block_count: Mapped[int] = mapped_column(default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
