"""Policy engine (M5): the pre-spend verdict at `authorize`, most-restrictive-wins.

The monthly budget is always enforced (never fail-open); evaluators are pure.
"""

from __future__ import annotations

import enum
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, TypeAdapter, field_validator

from lucnt_api.constants import MERCHANT_LIST_MAX, VENDOR_MAX
from lucnt_api.models.authorization import VerdictStatus
from lucnt_api.models.policy import Policy, PolicyRuleType


class VerdictCode(enum.StrEnum):
    """Machine-readable verdict reason; the SDK maps these to typed exceptions."""

    OK = "ok"
    BUDGET_EXHAUSTED = "budget_exhausted"
    MERCHANT_BLOCKED = "merchant_blocked"
    VELOCITY_EXCEEDED = "velocity_exceeded"
    AMOUNT_OVER_THRESHOLD = "amount_over_threshold"
    OUTSIDE_HOURS = "outside_hours"


# --- Typed rule configs (the "rule parser": config_json -> a validated model) ---


class BudgetCapConfig(BaseModel):
    """Additional daily cap layered on top of the always-on monthly budget."""

    model_config = {"extra": "forbid"}
    period: Literal["daily"] = "daily"
    limit: Decimal


class MerchantListConfig(BaseModel):
    """Allow- or block-list of vendors (case-insensitive).

    Bounded: it is scanned linearly on every `authorize()` (blocking money path).
    """

    model_config = {"extra": "forbid"}
    mode: Literal["allow", "block"]
    vendors: list[str] = Field(
        min_length=1,
        max_length=MERCHANT_LIST_MAX,
    )

    @field_validator("vendors")
    @classmethod
    def _check_vendor_lengths(cls, value: list[str]) -> list[str]:
        for vendor in value:
            if len(vendor) > VENDOR_MAX:
                raise ValueError(f"vendor entries must be <= {VENDOR_MAX} characters")
        return value


class VelocityConfig(BaseModel):
    """Cap on the number of spends within a rolling window."""

    model_config = {"extra": "forbid"}
    max_count: int = Field(ge=1)
    window: Literal["hour", "day"]


class AmountThresholdConfig(BaseModel):
    """Per-transaction ceiling; over it triggers `action` (default review)."""

    model_config = {"extra": "forbid"}
    max_amount: Decimal
    action: Literal["block", "review", "human_hold"] = "review"


class TimeOfDayConfig(BaseModel):
    """Allowed window `[start_hour, end_hour)` entity-local; start > end is overnight.

    `days` uses Python weekday numbers (Mon=0); `None` allows every day.
    """

    model_config = {"extra": "forbid"}
    start_hour: int = Field(ge=0, le=23)
    end_hour: int = Field(ge=0, le=24)
    days: list[int] | None = None


_CONFIG_BY_RULE: dict[PolicyRuleType, type[BaseModel]] = {
    PolicyRuleType.BUDGET_CAP: BudgetCapConfig,
    PolicyRuleType.MERCHANT_LIST: MerchantListConfig,
    PolicyRuleType.VELOCITY: VelocityConfig,
    PolicyRuleType.AMOUNT_THRESHOLD: AmountThresholdConfig,
    PolicyRuleType.TIME_OF_DAY: TimeOfDayConfig,
}


def parse_config(rule_type: PolicyRuleType, raw: dict[str, object]) -> BaseModel:
    """Validate a raw `config_json` against its rule type's schema.

    Raises:
        pydantic.ValidationError: Malformed config.
    """
    return TypeAdapter(_CONFIG_BY_RULE[rule_type]).validate_python(raw)


# --- Evaluation context + candidates ---


@dataclass(frozen=True)
class SpendContext:
    """Everything an evaluator needs about the spend under authorization."""

    amount: Decimal
    currency: str | None
    vendor: str
    now: datetime  # tz-aware, in the entity's local timezone
    agent_monthly_budget: Decimal
    agent_spent: Decimal  # accumulator compared to the monthly budget
    agent_spent_today: Decimal
    # Injected DB count of recent spends; keeps this module SQLAlchemy-free.
    recent_count: Callable[[timedelta], int]


@dataclass(frozen=True)
class _Candidate:
    """One rule's contribution to the verdict; `None` from an evaluator = approve."""

    status: VerdictStatus
    code: VerdictCode
    reason: str


@dataclass(frozen=True)
class Decision:
    """The final verdict plus the policies that fired (for hit/block counters)."""

    status: VerdictStatus
    code: VerdictCode
    reason: str | None
    fired_policy_ids: tuple[uuid.UUID, ...]
    blocking_policy_ids: tuple[uuid.UUID, ...]


_SEVERITY: dict[VerdictStatus, int] = {
    VerdictStatus.APPROVED: 0,
    VerdictStatus.REVIEW: 1,
    VerdictStatus.HUMAN_HOLD: 2,
    VerdictStatus.BLOCKED: 3,
}

_WINDOW: dict[str, timedelta] = {
    "hour": timedelta(hours=1),
    "day": timedelta(days=1),
}


# --- Pure per-rule evaluators (return a candidate if the rule fires, else None) ---


def _eval_monthly_budget(ctx: SpendContext) -> _Candidate | None:
    """Always-on invariant: block if projected month-to-date spend exceeds budget."""
    projected = ctx.agent_spent + ctx.amount
    if projected > ctx.agent_monthly_budget:
        return _Candidate(
            VerdictStatus.BLOCKED,
            VerdictCode.BUDGET_EXHAUSTED,
            f"projected spend {projected} exceeds monthly budget "
            f"{ctx.agent_monthly_budget}",
        )
    return None


def _eval_budget_cap(cfg: BudgetCapConfig, ctx: SpendContext) -> _Candidate | None:
    """Daily sub-limit on top of the monthly invariant."""
    projected_today = ctx.agent_spent_today + ctx.amount
    if projected_today > cfg.limit:
        return _Candidate(
            VerdictStatus.BLOCKED,
            VerdictCode.BUDGET_EXHAUSTED,
            f"projected daily spend {projected_today} exceeds daily cap {cfg.limit}",
        )
    return None


def _eval_merchant_list(
    cfg: MerchantListConfig, ctx: SpendContext
) -> _Candidate | None:
    """Block a listed vendor (block mode) or an unlisted one (allow mode)."""
    listed = {v.strip().lower() for v in cfg.vendors}
    vendor = ctx.vendor.strip().lower()
    if cfg.mode == "block" and vendor in listed:
        return _Candidate(
            VerdictStatus.BLOCKED,
            VerdictCode.MERCHANT_BLOCKED,
            f"vendor {ctx.vendor!r} is on the blocklist",
        )
    if cfg.mode == "allow" and vendor not in listed:
        return _Candidate(
            VerdictStatus.BLOCKED,
            VerdictCode.MERCHANT_BLOCKED,
            f"vendor {ctx.vendor!r} is not on the allowlist",
        )
    return None


def _eval_velocity(cfg: VelocityConfig, ctx: SpendContext) -> _Candidate | None:
    """Block once the count of recent spends reaches the window's cap."""
    count = ctx.recent_count(_WINDOW[cfg.window])
    if count >= cfg.max_count:
        return _Candidate(
            VerdictStatus.BLOCKED,
            VerdictCode.VELOCITY_EXCEEDED,
            f"{count} spends in the last {cfg.window} reaches the cap of "
            f"{cfg.max_count}",
        )
    return None


def _eval_amount_threshold(
    cfg: AmountThresholdConfig, ctx: SpendContext
) -> _Candidate | None:
    """Route a spend over the ceiling to block / review / human_hold."""
    if ctx.amount > cfg.max_amount:
        status = {
            "block": VerdictStatus.BLOCKED,
            "review": VerdictStatus.REVIEW,
            "human_hold": VerdictStatus.HUMAN_HOLD,
        }[cfg.action]
        return _Candidate(
            status,
            VerdictCode.AMOUNT_OVER_THRESHOLD,
            f"amount {ctx.amount} exceeds threshold {cfg.max_amount}",
        )
    return None


def _eval_time_of_day(cfg: TimeOfDayConfig, ctx: SpendContext) -> _Candidate | None:
    """Block spends outside the allowed hours / weekdays (entity-local time)."""
    if cfg.days is not None and ctx.now.weekday() not in cfg.days:
        return _Candidate(
            VerdictStatus.BLOCKED,
            VerdictCode.OUTSIDE_HOURS,
            f"spend on weekday {ctx.now.weekday()} is outside allowed days",
        )
    hour = ctx.now.hour
    if cfg.start_hour <= cfg.end_hour:
        allowed = cfg.start_hour <= hour < cfg.end_hour
    else:  # overnight window, e.g. 22 -> 6
        allowed = hour >= cfg.start_hour or hour < cfg.end_hour
    if not allowed:
        return _Candidate(
            VerdictStatus.BLOCKED,
            VerdictCode.OUTSIDE_HOURS,
            f"spend at hour {hour} is outside allowed window "
            f"[{cfg.start_hour}, {cfg.end_hour})",
        )
    return None


def _evaluate_policy(policy: Policy, ctx: SpendContext) -> _Candidate | None:
    """Run one policy's evaluator; `None` means the rule approved."""
    cfg = parse_config(policy.rule_type, policy.config_json)
    if isinstance(cfg, BudgetCapConfig):
        return _eval_budget_cap(cfg, ctx)
    if isinstance(cfg, MerchantListConfig):
        return _eval_merchant_list(cfg, ctx)
    if isinstance(cfg, VelocityConfig):
        return _eval_velocity(cfg, ctx)
    if isinstance(cfg, AmountThresholdConfig):
        return _eval_amount_threshold(cfg, ctx)
    if isinstance(cfg, TimeOfDayConfig):
        return _eval_time_of_day(cfg, ctx)
    raise AssertionError(f"unhandled policy config {type(cfg).__name__}")


def evaluate_spend(policies: Sequence[Policy], ctx: SpendContext) -> Decision:
    """Evaluate the budget invariant plus every policy; most restrictive wins.

    Args:
        policies: Active, already scope-filtered policies.
        ctx: The spend under authorization.
    """
    candidates: list[_Candidate] = []
    fired: list[uuid.UUID] = []
    blocking: list[uuid.UUID] = []

    base = _eval_monthly_budget(ctx)
    if base is not None:
        candidates.append(base)

    for policy in policies:
        candidate = _evaluate_policy(policy, ctx)
        if candidate is None:
            continue
        candidates.append(candidate)
        if policy.id is not None:
            fired.append(policy.id)
            if candidate.status == VerdictStatus.BLOCKED:
                blocking.append(policy.id)

    if not candidates:
        return Decision(
            VerdictStatus.APPROVED, VerdictCode.OK, None, tuple(fired), tuple(blocking)
        )

    winner = max(candidates, key=lambda c: _SEVERITY[c.status])
    return Decision(
        winner.status, winner.code, winner.reason, tuple(fired), tuple(blocking)
    )
