"""Unit tests for the pure policy engine (no DB, no session).

`Policy` ORM objects are constructed in memory — never flushed — so `policy.id`
is `None`; these tests assert on the verdict logic, while hit/block-counter
bumping (which needs real ids) is covered in the integration tests (Phase 3c).
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from lucnt_api.models.authorization import VerdictStatus
from lucnt_api.models.policy import Policy, PolicyRuleType
from lucnt_api.services.policy_engine import (
    SpendContext,
    VerdictCode,
    evaluate_spend,
    parse_config,
)

# A Monday at 14:00 UTC — comfortably inside a 9→18 business-hours window.
_MONDAY_1400 = datetime(2026, 7, 6, 14, 0, tzinfo=timezone.utc)


def _ctx(
    *,
    amount: str = "10",
    vendor: str = "openai",
    currency: str | None = "USD",
    now: datetime = _MONDAY_1400,
    monthly_budget: str = "1000",
    spent: str = "0",
    spent_today: str = "0",
    recent: int = 0,
) -> SpendContext:
    """Build a SpendContext with sensible, non-firing defaults."""
    return SpendContext(
        amount=Decimal(amount),
        currency=currency,
        vendor=vendor,
        now=now,
        agent_monthly_budget=Decimal(monthly_budget),
        agent_spent=Decimal(spent),
        agent_spent_today=Decimal(spent_today),
        recent_count=lambda _window: recent,
    )


def _policy(rule_type: PolicyRuleType, config: dict[str, object]) -> Policy:
    """An in-memory (unflushed) policy carrying a raw config_json."""
    return Policy(rule_type=rule_type, config_json=config)


def test_no_policies_within_budget_is_approved() -> None:
    """The empty ruleset with spend under budget approves with code ok."""
    d = evaluate_spend([], _ctx(amount="10", monthly_budget="1000"))
    assert d.status == VerdictStatus.APPROVED
    assert d.code == VerdictCode.OK
    assert d.reason is None


def test_monthly_budget_invariant_always_enforced() -> None:
    """Budget is a base invariant — it blocks with no policy row present."""
    d = evaluate_spend([], _ctx(amount="200", monthly_budget="1000", spent="900"))
    assert d.status == VerdictStatus.BLOCKED
    assert d.code == VerdictCode.BUDGET_EXHAUSTED


def test_daily_budget_cap_blocks() -> None:
    """A budget_cap policy caps daily spend on top of the monthly invariant."""
    pol = _policy(PolicyRuleType.BUDGET_CAP, {"period": "daily", "limit": "50"})
    d = evaluate_spend([pol], _ctx(amount="20", spent_today="40", monthly_budget="1e9"))
    assert d.status == VerdictStatus.BLOCKED
    assert d.code == VerdictCode.BUDGET_EXHAUSTED


def test_merchant_blocklist_blocks_listed_vendor() -> None:
    """block mode blocks a listed vendor, case-insensitively."""
    pol = _policy(
        PolicyRuleType.MERCHANT_LIST, {"mode": "block", "vendors": ["Coinbase"]}
    )
    d = evaluate_spend([pol], _ctx(vendor="coinbase"))
    assert d.status == VerdictStatus.BLOCKED
    assert d.code == VerdictCode.MERCHANT_BLOCKED


def test_merchant_allowlist_blocks_unlisted_vendor() -> None:
    """allow mode blocks anything not on the list."""
    pol = _policy(
        PolicyRuleType.MERCHANT_LIST, {"mode": "allow", "vendors": ["openai"]}
    )
    blocked = evaluate_spend([pol], _ctx(vendor="anthropic"))
    assert blocked.status == VerdictStatus.BLOCKED
    assert evaluate_spend([pol], _ctx(vendor="openai")).status == VerdictStatus.APPROVED


def test_velocity_blocks_at_cap() -> None:
    """Velocity blocks once recent count reaches max_count."""
    pol = _policy(PolicyRuleType.VELOCITY, {"max_count": 5, "window": "hour"})
    assert evaluate_spend([pol], _ctx(recent=5)).code == VerdictCode.VELOCITY_EXCEEDED
    assert evaluate_spend([pol], _ctx(recent=4)).status == VerdictStatus.APPROVED


def test_amount_threshold_actions() -> None:
    """Over-threshold routes to the configured action; under-threshold approves."""
    for action, expected in (
        ("block", VerdictStatus.BLOCKED),
        ("review", VerdictStatus.REVIEW),
        ("human_hold", VerdictStatus.HUMAN_HOLD),
    ):
        pol = _policy(
            PolicyRuleType.AMOUNT_THRESHOLD,
            {"max_amount": "100", "action": action},
        )
        over = evaluate_spend([pol], _ctx(amount="150", monthly_budget="1e9"))
        assert over.status == expected
        assert over.code == VerdictCode.AMOUNT_OVER_THRESHOLD
        under = evaluate_spend([pol], _ctx(amount="50", monthly_budget="1e9"))
        assert under.status == VerdictStatus.APPROVED


def test_time_of_day_business_hours() -> None:
    """A 9→18 window blocks a 22:00 spend but allows a 14:00 one."""
    pol = _policy(PolicyRuleType.TIME_OF_DAY, {"start_hour": 9, "end_hour": 18})
    night = _MONDAY_1400.replace(hour=22)
    assert evaluate_spend([pol], _ctx(now=night)).code == VerdictCode.OUTSIDE_HOURS
    day = evaluate_spend([pol], _ctx(now=_MONDAY_1400))
    assert day.status == VerdictStatus.APPROVED


def test_time_of_day_overnight_window() -> None:
    """An overnight 22→6 window allows 02:00 and blocks 14:00."""
    pol = _policy(PolicyRuleType.TIME_OF_DAY, {"start_hour": 22, "end_hour": 6})
    early = _MONDAY_1400.replace(hour=2)
    assert evaluate_spend([pol], _ctx(now=early)).status == VerdictStatus.APPROVED
    day = evaluate_spend([pol], _ctx(now=_MONDAY_1400))
    assert day.code == VerdictCode.OUTSIDE_HOURS


def test_time_of_day_weekday_restriction() -> None:
    """Restricting to weekdays blocks a spend on an excluded day."""
    # _MONDAY_1400.weekday() == 0; allow only Tue–Fri (1–4) => Monday is blocked.
    pol = _policy(
        PolicyRuleType.TIME_OF_DAY,
        {"start_hour": 0, "end_hour": 24, "days": [1, 2, 3, 4]},
    )
    assert evaluate_spend([pol], _ctx()).code == VerdictCode.OUTSIDE_HOURS


def test_most_restrictive_wins() -> None:
    """When several rules fire, blocked beats review beats approved."""
    review = _policy(
        PolicyRuleType.AMOUNT_THRESHOLD, {"max_amount": "5", "action": "review"}
    )
    block = _policy(
        PolicyRuleType.MERCHANT_LIST, {"mode": "block", "vendors": ["openai"]}
    )
    d = evaluate_spend([review, block], _ctx(amount="150", vendor="openai"))
    assert d.status == VerdictStatus.BLOCKED
    assert d.code == VerdictCode.MERCHANT_BLOCKED


def test_review_alone_is_not_blocked() -> None:
    """A lone review verdict is returned as review, not escalated to blocked."""
    review = _policy(
        PolicyRuleType.AMOUNT_THRESHOLD, {"max_amount": "5", "action": "review"}
    )
    d = evaluate_spend([review], _ctx(amount="150", monthly_budget="1e9"))
    assert d.status == VerdictStatus.REVIEW


def test_parse_config_rejects_malformed() -> None:
    """A config missing required fields raises, so a broken rule never persists."""
    with pytest.raises(ValidationError):
        parse_config(PolicyRuleType.VELOCITY, {"window": "hour"})  # no max_count
    with pytest.raises(ValidationError):
        parse_config(PolicyRuleType.MERCHANT_LIST, {"mode": "block", "vendors": []})
