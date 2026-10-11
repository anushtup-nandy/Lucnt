"""Agent financial intelligence (M8): pure, DB-free computations over the ledger.

Amount stats use a single (home) currency; summing across currencies is meaningless.
"""

from __future__ import annotations

import calendar
import uuid
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

# --- Intelligence defaults (business constants) ---
_MIN_FINGERPRINT_SAMPLES = 3  # min home-currency spends to establish a baseline
_RECENT_WINDOW = 10  # trailing spends the anomaly score scans
_TOP_VENDORS = 3
ANOMALY_Z_THRESHOLD = Decimal("2.5")  # z at/above which a spend reads "anomalous"
# Finite cap: a flat baseline has zero spread, so any differing spend is ∞-z.
_MAX_Z = Decimal("10")
# Floors the run-rate divisor so one day-1 renewal doesn't project to ~30x.
_MIN_PROJECTION_DAYS = 7
_UNATTRIBUTED = "(unattributed)"
_MONEY = Decimal("0.0001")  # ledger precision, Numeric(18, 4)
_SCORE = Decimal("0.0001")


@dataclass(frozen=True)
class SpendRecord:
    """Minimal transaction projection; amounts are entity-local for comparability."""

    agent_id: uuid.UUID
    vendor: str
    amount: Decimal
    currency: str
    project: str | None
    model_used: str | None
    tokens: int | None
    transaction_date: datetime  # tz-aware


@dataclass(frozen=True)
class AmountStats:
    """Univariate summary of an agent's spend amounts in one currency."""

    currency: str
    sample_size: int
    mean: Decimal
    stddev: Decimal
    median: Decimal
    minimum: Decimal
    maximum: Decimal


@dataclass(frozen=True)
class VendorShare:
    """One vendor's share of an agent's transaction count (not amount)."""

    vendor: str
    count: int
    share: Decimal  # 0..1


@dataclass(frozen=True)
class FingerprintFeatures:
    """An agent's learned spend fingerprint (persisted to `fingerprints`)."""

    enough_data: bool
    home_currency: str | None
    total_records: int
    amount_stats: AmountStats | None
    top_vendors: list[VendorShare]
    active_hours: dict[int, int]  # hour-of-day -> count (all records)
    daily_velocity: Decimal  # avg records/day across the observed span
    anomaly_score: Decimal  # max recent z-score (0 when not computable)
    # Full vendor history (not just top N) — the "known vendor" set for scoring.
    known_vendors: frozenset[str] = frozenset()

    def as_feature_vector(self) -> dict[str, object]:
        """JSON-serializable feature vector for `fingerprints.feature_vector`."""
        stats = self.amount_stats
        return {
            "enough_data": self.enough_data,
            "home_currency": self.home_currency,
            "total_records": self.total_records,
            "amount_stats": (
                {
                    "currency": stats.currency,
                    "sample_size": stats.sample_size,
                    "mean": str(stats.mean),
                    "stddev": str(stats.stddev),
                    "median": str(stats.median),
                    "min": str(stats.minimum),
                    "max": str(stats.maximum),
                }
                if stats is not None
                else None
            ),
            "top_vendors": [
                {"vendor": v.vendor, "count": v.count, "share": str(v.share)}
                for v in self.top_vendors
            ],
            "active_hours": {str(h): c for h, c in sorted(self.active_hours.items())},
            "daily_velocity": str(self.daily_velocity),
            "anomaly_score": str(self.anomaly_score),
            "known_vendors": sorted(self.known_vendors),
        }


@dataclass(frozen=True)
class AnomalyScore:
    """The result of scoring one candidate spend against a fingerprint."""

    z_score: Decimal | None  # None when currency != home or the baseline is flat
    unknown_vendor: bool
    is_anomalous: bool


@dataclass(frozen=True)
class CostUnit:
    """Cost-per-output for one unit of work (a `project`), in one currency."""

    unit: str
    dimension: str  # currently always "project"
    currency: str
    total_spent: Decimal
    count: int
    avg_cost: Decimal
    total_tokens: int | None
    cost_per_1k_tokens: Decimal | None


@dataclass(frozen=True)
class SpendProjection:
    """A month-end spend projection from the current run-rate vs the budget."""

    month_to_date: Decimal
    projected_month_end: Decimal
    monthly_budget: Decimal
    on_track: bool
    projected_overrun: Decimal
    day_of_month: int
    days_in_month: int


# --- Numeric helpers (Decimal-exact; no float coercion) ---


def _q_money(value: Decimal) -> Decimal:
    return value.quantize(_MONEY, rounding=ROUND_HALF_UP)


def _q_score(value: Decimal) -> Decimal:
    return value.quantize(_SCORE, rounding=ROUND_HALF_UP)


def _mean(values: Sequence[Decimal]) -> Decimal:
    return sum(values, Decimal("0")) / Decimal(len(values))


def _median(values: Sequence[Decimal]) -> Decimal:
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2 == 1:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def _stddev(values: Sequence[Decimal], mean: Decimal) -> Decimal:
    """Population standard deviation, Decimal-exact via ``Decimal.sqrt``."""
    variance = sum(((v - mean) ** 2 for v in values), Decimal("0")) / Decimal(
        len(values)
    )
    return variance.sqrt()


def amount_stats(amounts: Sequence[Decimal], currency: str) -> AmountStats:
    """Summarize a non-empty sequence of same-currency amounts."""
    mean = _mean(amounts)
    return AmountStats(
        currency=currency,
        sample_size=len(amounts),
        mean=_q_money(mean),
        stddev=_q_money(_stddev(amounts, mean)),
        median=_q_money(_median(amounts)),
        minimum=_q_money(min(amounts)),
        maximum=_q_money(max(amounts)),
    )


# --- Fingerprint ---


def _home_currency(records: Sequence[SpendRecord]) -> str:
    """Modal currency of non-empty ``records``; ties go to the largest total."""
    totals: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    counts: Counter[str] = Counter()
    for r in records:
        counts[r.currency] += 1
        totals[r.currency] += r.amount
    return max(counts, key=lambda c: (counts[c], totals[c]))


def compute_fingerprint(
    records: Sequence[SpendRecord],
    *,
    home_currency: str | None = None,
    now: datetime | None = None,
) -> FingerprintFeatures:
    """Learn one agent's spend fingerprint from its transaction history.

    Amount stats and anomaly score use home-currency spends only (mixing
    currencies is meaningless); other features use every record.

    Args:
        records: The agent's committed spends.
        home_currency: Pin the home currency so it can't flip on a count tie;
            defaults to the modal currency.
        now: Velocity span end, so a dormant agent's velocity decays.
    """
    if not records:
        return FingerprintFeatures(
            enough_data=False,
            home_currency=home_currency,
            total_records=0,
            amount_stats=None,
            top_vendors=[],
            active_hours={},
            daily_velocity=Decimal("0"),
            anomaly_score=Decimal("0"),
            known_vendors=frozenset(),
        )

    home = home_currency if home_currency is not None else _home_currency(records)
    total = len(records)
    known_vendors = frozenset(r.vendor for r in records)

    vendor_counts = Counter(r.vendor for r in records)
    top_vendors = [
        VendorShare(
            vendor=vendor,
            count=count,
            share=_q_score(Decimal(count) / Decimal(total)),
        )
        for vendor, count in vendor_counts.most_common(_TOP_VENDORS)
    ]

    active_hours: dict[int, int] = dict(
        Counter(r.transaction_date.hour for r in records)
    )
    dates = [r.transaction_date for r in records]
    span_end = now if now is not None else max(dates)
    span_days = max((span_end - min(dates)).days + 1, 1)
    daily_velocity = _q_score(Decimal(total) / Decimal(span_days))

    home_records = sorted(
        (r for r in records if r.currency == home),
        key=lambda r: r.transaction_date,
    )
    home_amounts = [r.amount for r in home_records]
    if len(home_amounts) < _MIN_FINGERPRINT_SAMPLES:
        return FingerprintFeatures(
            enough_data=False,
            home_currency=home,
            total_records=total,
            amount_stats=None,
            top_vendors=top_vendors,
            active_hours=active_hours,
            daily_velocity=daily_velocity,
            anomaly_score=Decimal("0"),
            known_vendors=known_vendors,
        )

    stats = amount_stats(home_amounts, home)
    anomaly = _recent_anomaly_score(home_amounts)
    return FingerprintFeatures(
        enough_data=True,
        home_currency=home,
        total_records=total,
        amount_stats=stats,
        top_vendors=top_vendors,
        active_hours=active_hours,
        daily_velocity=daily_velocity,
        anomaly_score=anomaly,
        known_vendors=known_vendors,
    )


def _leave_one_out_z(amounts: Sequence[Decimal], idx: int) -> Decimal | None:
    """Leave-one-out z of ``amounts[idx]``, so an outlier can't mute itself.

    None without a baseline; capped at ``_MAX_Z``.
    """
    others = [a for i, a in enumerate(amounts) if i != idx]
    if len(others) < 2:
        return None
    mean = _mean(others)
    sd = _stddev(others, mean)
    candidate = amounts[idx]
    if sd == 0:
        return Decimal("0") if candidate == mean else _MAX_Z
    return min(abs(candidate - mean) / sd, _MAX_Z)


def _recent_anomaly_score(ordered_amounts: Sequence[Decimal]) -> Decimal:
    """Max leave-one-out z among the trailing ``_RECENT_WINDOW`` spends (0 if none)."""
    n = len(ordered_amounts)
    start = max(0, n - _RECENT_WINDOW)
    zs = [
        z
        for i in range(start, n)
        if (z := _leave_one_out_z(ordered_amounts, i)) is not None
    ]
    return _q_score(max(zs)) if zs else Decimal("0")


def score_anomaly(
    features: FingerprintFeatures,
    *,
    amount: Decimal,
    currency: str,
    vendor: str,
) -> AnomalyScore:
    """Score a candidate spend against a learned fingerprint.

    ``z_score`` is None without a same-currency, non-flat baseline.
    ``is_anomalous`` fires on z ≥ threshold or a vendor never seen before.
    """
    unknown_vendor = vendor not in features.known_vendors
    stats = features.amount_stats
    if stats is None or currency != features.home_currency or stats.stddev <= 0:
        return AnomalyScore(
            z_score=None,
            unknown_vendor=unknown_vendor,
            is_anomalous=unknown_vendor,
        )
    z = _q_score(abs(amount - stats.mean) / stats.stddev)
    return AnomalyScore(
        z_score=z,
        unknown_vendor=unknown_vendor,
        is_anomalous=z >= ANOMALY_Z_THRESHOLD or unknown_vendor,
    )


# --- Cost-per-output ---


def cost_per_output(records: Sequence[SpendRecord]) -> list[CostUnit]:
    """Cost per ``project`` and currency, dearest first.

    Token metrics only when every record in the group has a token count.
    """
    groups: dict[tuple[str, str], list[SpendRecord]] = defaultdict(list)
    for r in records:
        unit = r.project if r.project else _UNATTRIBUTED
        groups[(unit, r.currency)].append(r)

    units: list[CostUnit] = []
    for (unit, currency), members in groups.items():
        total_spent = sum((m.amount for m in members), Decimal("0"))
        count = len(members)
        avg_cost = total_spent / Decimal(count)
        all_have_tokens = all(m.tokens is not None for m in members)
        total_tokens = sum(m.tokens or 0 for m in members) if all_have_tokens else None
        cost_per_1k = (
            _q_money(total_spent / Decimal(total_tokens) * 1000)
            if total_tokens
            else None
        )
        units.append(
            CostUnit(
                unit=unit,
                dimension="project",
                currency=currency,
                total_spent=_q_money(total_spent),
                count=count,
                avg_cost=_q_money(avg_cost),
                total_tokens=total_tokens,
                cost_per_1k_tokens=cost_per_1k,
            )
        )
    units.sort(key=lambda u: u.avg_cost, reverse=True)
    return units


# --- Spend projection (predictive intelligence) ---


def project_spend(
    *, month_to_date: Decimal, now: datetime, monthly_budget: Decimal
) -> SpendProjection:
    """Project month-end spend from a floored linear run-rate vs the budget.

    ``now`` must be entity-local so month boundaries flip at local midnight.
    """
    day_of_month = now.day
    days_in_month = calendar.monthrange(now.year, now.month)[1]
    effective_days = max(day_of_month, _MIN_PROJECTION_DAYS)
    run_rate = month_to_date / Decimal(effective_days)
    projected = max(
        _q_money(month_to_date), _q_money(run_rate * Decimal(days_in_month))
    )
    if monthly_budget > 0:
        overrun = projected - monthly_budget
        on_track = overrun <= 0
        projected_overrun = _q_money(max(overrun, Decimal("0")))
    else:
        on_track = True
        projected_overrun = Decimal("0")
    return SpendProjection(
        month_to_date=_q_money(month_to_date),
        projected_month_end=projected,
        monthly_budget=monthly_budget,
        on_track=on_track,
        projected_overrun=projected_overrun,
        day_of_month=day_of_month,
        days_in_month=days_in_month,
    )
