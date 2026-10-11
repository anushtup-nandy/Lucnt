"""Pure reconciliation detector tests (in-memory `TxnView`s, no DB)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from lucnt_api.models.reconciliation_exception import ReconExceptionType
from lucnt_api.services.detectors import (
    TxnView,
    detect_duplicates,
    detect_overcharges,
    detect_unused_subscriptions,
    run_detectors,
)

_BASE = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
_AGENT_A = uuid.UUID(int=0xA)
_AGENT_B = uuid.UUID(int=0xB)


def _txn(
    *,
    agent_id: uuid.UUID = _AGENT_A,
    vendor: str = "openai",
    amount: str = "10",
    currency: str = "USD",
    when: datetime = _BASE,
    txn_id: uuid.UUID | None = None,
) -> TxnView:
    """Build a TxnView with sensible defaults."""
    return TxnView(
        id=txn_id if txn_id is not None else uuid.uuid4(),
        agent_id=agent_id,
        vendor=vendor,
        paid_amount=Decimal(amount),
        paid_currency=currency,
        transaction_date=when,
    )


# --- duplicates ---


def test_duplicate_within_window_flags_the_later_charge() -> None:
    """Two identical charges 2 min apart flag the later one for the full amount."""
    first = _txn(when=_BASE)
    second = _txn(when=_BASE + timedelta(minutes=2))
    hits = detect_duplicates([first, second])
    assert len(hits) == 1
    hit = hits[0]
    assert hit.transaction_id == second.id
    assert hit.exception_type == ReconExceptionType.DUPLICATE
    assert hit.amount_at_stake == Decimal("10")
    assert hit.evidence["original_transaction_id"] == str(first.id)
    assert hit.evidence["window_minutes"] == 5  # int, not a float ratio


def test_duplicate_outside_window_is_not_flagged() -> None:
    """Identical charges more than 5 min apart are not duplicates."""
    hits = detect_duplicates(
        [_txn(when=_BASE), _txn(when=_BASE + timedelta(minutes=6))]
    )
    assert hits == []


def test_duplicate_at_exact_window_boundary_not_flagged() -> None:
    """A gap of *exactly* the window is a cadence, not a double-charge."""
    hits = detect_duplicates(
        [_txn(when=_BASE), _txn(when=_BASE + timedelta(minutes=5))]
    )
    assert hits == []


def test_three_identical_within_window_flag_two() -> None:
    """The first is the original; the two later ones are both duplicates."""
    txns = [
        _txn(when=_BASE),
        _txn(when=_BASE + timedelta(minutes=1)),
        _txn(when=_BASE + timedelta(minutes=2)),
    ]
    hits = detect_duplicates(txns)
    assert len(hits) == 2
    assert {h.transaction_id for h in hits} == {txns[1].id, txns[2].id}


def test_duplicate_ignores_different_vendor_amount_agent() -> None:
    """Grouping is on (agent, vendor, amount, currency); differences don't match."""
    base = _txn(when=_BASE)
    assert detect_duplicates([base, _txn(vendor="anthropic", when=_BASE)]) == []
    assert detect_duplicates([base, _txn(amount="11", when=_BASE)]) == []
    assert detect_duplicates([base, _txn(agent_id=_AGENT_B, when=_BASE)]) == []


def test_duplicate_case_insensitive_vendor() -> None:
    """Vendor matching is case/space-insensitive."""
    hits = detect_duplicates(
        [_txn(vendor="OpenAI", when=_BASE), _txn(vendor="openai ", when=_BASE)]
    )
    assert len(hits) == 1


# --- overcharges ---


def test_overcharge_flags_statistical_outlier() -> None:
    """A charge >= 3x the (agent, vendor) median is flagged for the overage."""
    txns = [_txn(amount="10"), _txn(amount="10"), _txn(amount="10"), _txn(amount="100")]
    hits = detect_overcharges(txns)
    assert len(hits) == 1
    assert hits[0].amount_at_stake == Decimal("90")  # 100 - median(10)
    assert hits[0].evidence["median"] == "10"


def test_overcharge_needs_minimum_samples() -> None:
    """Fewer than min_samples charges gives no baseline — nothing flagged."""
    assert detect_overcharges([_txn(amount="10"), _txn(amount="100")]) == []


def test_overcharge_requires_baseline_excluding_candidate() -> None:
    """Leave-one-out: a 3-charge group's 2-charge baseline is below min_samples."""
    txns = [_txn(amount="10"), _txn(amount="10"), _txn(amount="100")]
    assert detect_overcharges(txns) == []


def test_overcharge_does_not_mix_currencies() -> None:
    """A lone foreign-currency charge is not judged against a USD baseline."""
    txns = [
        _txn(amount="10", currency="USD"),
        _txn(amount="10", currency="USD"),
        _txn(amount="10", currency="USD"),
        _txn(amount="1500", currency="JPY"),
    ]
    assert detect_overcharges(txns) == []


def test_overcharge_ignores_identical_recurring_charges() -> None:
    """A subscription (all-identical) has median == amount; never an overcharge."""
    assert detect_overcharges([_txn(amount="50") for _ in range(4)]) == []


def test_overcharge_below_factor_not_flagged() -> None:
    """Above the median but under 3x is within normal variation."""
    txns = [_txn(amount="10"), _txn(amount="10"), _txn(amount="10"), _txn(amount="25")]
    assert detect_overcharges(txns) == []


# --- unused subscriptions ---


def test_unused_subscription_flags_latest_of_monthly_recurring() -> None:
    """Three identical ~monthly charges flag the latest occurrence."""
    txns = [
        _txn(amount="99", when=_BASE),
        _txn(amount="99", when=_BASE + timedelta(days=30)),
        _txn(amount="99", when=_BASE + timedelta(days=60)),
    ]
    hits = detect_unused_subscriptions(txns)
    assert len(hits) == 1
    assert hits[0].transaction_id == txns[-1].id
    assert hits[0].exception_type == ReconExceptionType.UNUSED_SUBSCRIPTION
    assert hits[0].amount_at_stake == Decimal("99")
    assert hits[0].evidence["occurrences"] == 3


def test_unused_subscription_needs_minimum_occurrences() -> None:
    """Two monthly charges is not yet a recurring pattern."""
    txns = [
        _txn(amount="99", when=_BASE),
        _txn(amount="99", when=_BASE + timedelta(days=30)),
    ]
    assert detect_unused_subscriptions(txns) == []


def test_unused_subscription_ignores_non_monthly_cadence() -> None:
    """Daily-cadence identical charges are not a monthly subscription."""
    txns = [_txn(amount="99", when=_BASE + timedelta(days=d)) for d in (0, 1, 2)]
    assert detect_unused_subscriptions(txns) == []


def test_unused_subscription_requires_consecutive_monthly_run() -> None:
    """Gaps [30, 5, 30]: two monthly gaps but no consecutive run, so no flag."""
    txns = [_txn(amount="99", when=_BASE + timedelta(days=d)) for d in (0, 30, 35, 65)]
    assert detect_unused_subscriptions(txns) == []


def test_unused_subscription_rounds_gaps_not_truncates() -> None:
    """A 24.6-day gap rounds to 25 (in the monthly window), not 24 (out)."""
    step = timedelta(days=24, hours=15)  # 24.625 days -> rounds to 25
    txns = [_txn(amount="99", when=_BASE + step * i) for i in range(3)]
    hits = detect_unused_subscriptions(txns)
    assert len(hits) == 1
    assert hits[0].evidence["cadence_days_observed"] == [25, 25]


# --- run_detectors (combined) ---


def test_run_detectors_combines_all_signals() -> None:
    """run_detectors surfaces duplicate + overcharge + subscription hits together."""
    duplicate = [_txn(vendor="stripe", amount="5", when=_BASE) for _ in range(2)]
    overcharge = [
        _txn(vendor="aws", amount="10"),
        _txn(vendor="aws", amount="10"),
        _txn(vendor="aws", amount="10"),
        _txn(vendor="aws", amount="100"),
    ]
    subscription = [
        _txn(vendor="notion", amount="8", when=_BASE + timedelta(days=30 * i))
        for i in range(3)
    ]
    hits = run_detectors([*duplicate, *overcharge, *subscription])
    kinds = {h.exception_type for h in hits}
    assert kinds == {
        ReconExceptionType.DUPLICATE,
        ReconExceptionType.OVERCHARGE,
        ReconExceptionType.UNUSED_SUBSCRIPTION,
    }
