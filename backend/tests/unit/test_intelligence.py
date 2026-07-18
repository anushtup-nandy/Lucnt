"""Unit tests for the pure intelligence service (Phase 5, M8).

Amounts are chosen so the statistics are exact: ``[10, 10, 10, 10, 50]`` has
mean 18 and population σ 16 (variance = (4·8² + 32²)/5 = 256), so no float
tolerance is needed anywhere.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from lucnt_api.services.intelligence import (
    FingerprintFeatures,
    SpendRecord,
    compute_fingerprint,
    cost_per_output,
    project_spend,
    score_anomaly,
)

_NOW = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
_AGENT = uuid.uuid4()


def _record(
    *,
    amount: str,
    vendor: str = "aws",
    currency: str = "USD",
    project: str | None = None,
    tokens: int | None = None,
    day: int = 0,
) -> SpendRecord:
    return SpendRecord(
        agent_id=_AGENT,
        vendor=vendor,
        amount=Decimal(amount),
        currency=currency,
        project=project,
        model_used=None,
        tokens=tokens,
        transaction_date=_NOW + timedelta(days=day),
    )


# --- compute_fingerprint ---


def test_fingerprint_empty_history() -> None:
    fp = compute_fingerprint([])
    assert fp.enough_data is False
    assert fp.home_currency is None
    assert fp.total_records == 0
    assert fp.amount_stats is None


def test_fingerprint_too_few_samples() -> None:
    fp = compute_fingerprint([_record(amount="10"), _record(amount="20")])
    assert fp.enough_data is False
    assert fp.home_currency == "USD"
    assert fp.amount_stats is None  # < 3 home-currency samples


def test_fingerprint_exact_statistics() -> None:
    amounts = ["10", "10", "10", "10", "50"]
    records = [_record(amount=a, day=i) for i, a in enumerate(amounts)]
    fp = compute_fingerprint(records)
    assert fp.enough_data is True
    assert fp.home_currency == "USD"
    stats = fp.amount_stats
    assert stats is not None
    assert stats.sample_size == 5
    assert stats.mean == Decimal("18.0000")
    assert stats.median == Decimal("10.0000")
    assert stats.stddev == Decimal("16.0000")  # exact: sqrt(256)
    assert stats.minimum == Decimal("10.0000")
    assert stats.maximum == Decimal("50.0000")
    # max recent z = |50 - 18| / 16 = 2.0
    assert fp.anomaly_score == Decimal("2.0000")


def test_fingerprint_top_vendors_by_count() -> None:
    records = [
        _record(amount="10", vendor="aws"),
        _record(amount="10", vendor="aws"),
        _record(amount="10", vendor="openai"),
        _record(amount="10", vendor="stripe"),
    ]
    fp = compute_fingerprint(records)
    assert fp.top_vendors[0].vendor == "aws"
    assert fp.top_vendors[0].count == 2
    assert fp.top_vendors[0].share == Decimal("0.5000")


def test_fingerprint_home_currency_is_modal() -> None:
    # 3 USD, 1 EUR -> USD is home; the EUR spend is excluded from amount stats.
    records = [
        _record(amount="10", currency="USD"),
        _record(amount="10", currency="USD"),
        _record(amount="10", currency="USD"),
        _record(amount="9999", currency="EUR"),
    ]
    fp = compute_fingerprint(records)
    assert fp.home_currency == "USD"
    assert fp.total_records == 4
    stats = fp.amount_stats
    assert stats is not None
    assert stats.sample_size == 3  # EUR outlier excluded
    assert stats.maximum == Decimal("10.0000")


def test_fingerprint_flat_history_has_zero_anomaly() -> None:
    fp = compute_fingerprint([_record(amount="10") for _ in range(4)])
    assert fp.amount_stats is not None
    assert fp.amount_stats.stddev == Decimal("0.0000")
    assert fp.anomaly_score == Decimal("0")  # no spread -> nothing anomalous


# --- score_anomaly ---


def _baseline() -> FingerprintFeatures:
    records = [_record(amount=a) for a in ["10", "10", "10", "10", "50"]]
    return compute_fingerprint(records)


def test_score_anomaly_outlier() -> None:
    fp = _baseline()  # mean 18, σ 16
    result = score_anomaly(fp, amount=Decimal("100"), currency="USD", vendor="aws")
    assert result.z_score == Decimal("5.1250")  # |100 - 18| / 16
    assert result.unknown_vendor is False
    assert result.is_anomalous is True


def test_score_anomaly_within_baseline() -> None:
    fp = _baseline()
    result = score_anomaly(fp, amount=Decimal("18"), currency="USD", vendor="aws")
    assert result.z_score == Decimal("0.0000")
    assert result.is_anomalous is False


def test_score_anomaly_unknown_vendor_flags_even_when_normal() -> None:
    fp = _baseline()
    result = score_anomaly(fp, amount=Decimal("18"), currency="USD", vendor="mystery")
    assert result.unknown_vendor is True
    assert result.is_anomalous is True


def test_score_anomaly_cross_currency_has_no_zscore() -> None:
    fp = _baseline()
    result = score_anomaly(fp, amount=Decimal("18"), currency="EUR", vendor="aws")
    assert result.z_score is None


# --- cost_per_output ---


def test_cost_per_output_groups_and_sorts() -> None:
    records = [
        _record(amount="10", project="report", tokens=1000),
        _record(amount="10", project="report", tokens=1000),
        _record(amount="100", project="leadgen"),
        _record(amount="5", project=None),  # -> "(unattributed)"
    ]
    units = cost_per_output(records)
    # sorted by avg_cost desc: leadgen (100) > report (10) > unattributed (5)
    assert [u.unit for u in units] == ["leadgen", "report", "(unattributed)"]

    report = next(u for u in units if u.unit == "report")
    assert report.total_spent == Decimal("20.0000")
    assert report.count == 2
    assert report.avg_cost == Decimal("10.0000")
    assert report.total_tokens == 2000
    assert report.cost_per_1k_tokens == Decimal("10.0000")  # 20 / 2000 * 1000

    leadgen = next(u for u in units if u.unit == "leadgen")
    assert leadgen.total_tokens is None  # no token count on the record


def test_cost_per_output_empty() -> None:
    assert cost_per_output([]) == []


# --- project_spend ---


def test_project_spend_over_budget() -> None:
    # day 10 of a 30-day month, $300 so far -> run-rate $30/day -> $900 projected.
    proj = project_spend(
        month_to_date=Decimal("300"),
        now=datetime(2026, 6, 10, tzinfo=timezone.utc),
        monthly_budget=Decimal("600"),
    )
    assert proj.day_of_month == 10
    assert proj.days_in_month == 30
    assert proj.projected_month_end == Decimal("900.0000")
    assert proj.on_track is False
    assert proj.projected_overrun == Decimal("300.0000")


def test_project_spend_on_track() -> None:
    proj = project_spend(
        month_to_date=Decimal("300"),
        now=datetime(2026, 6, 10, tzinfo=timezone.utc),
        monthly_budget=Decimal("1000"),
    )
    assert proj.on_track is True
    assert proj.projected_overrun == Decimal("0")


def test_project_spend_no_budget_never_over() -> None:
    proj = project_spend(
        month_to_date=Decimal("300"),
        now=datetime(2026, 6, 10, tzinfo=timezone.utc),
        monthly_budget=Decimal("0"),
    )
    assert proj.on_track is True
    assert proj.projected_overrun == Decimal("0")
    assert proj.projected_month_end == Decimal("900.0000")
