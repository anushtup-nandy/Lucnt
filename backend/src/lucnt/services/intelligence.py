"""Agent financial intelligence (Phase 5, M8) — pure computations over the ledger.

Mirrors `services/detectors.py` / `services/policy_engine.py`: every function here
is pure over a lightweight `SpendRecord` value object, so this module imports no
SQLAlchemy and is unit-testable without a database. The intelligence router loads
committed transactions, projects them to `SpendRecord`s, and runs these to build
fingerprints, cost-per-output, and spend projections; `services/insight_generator.py`
turns the results into persisted insights/memories.

Honesty about computable signals (the recovery-detector discipline applies here):

- **Amount statistics** (mean/stddev/median, and therefore anomaly z-scores) are
  computed in a *single* currency — the agent's modal ("home") currency — because
  summing across currencies is meaningless. Records in other currencies still count
  toward vendor/hour/velocity features. FX normalization is a separate backend gap
  (``local_amount`` currently mirrors ``paid_amount``); when it lands, feeding a
  truly normalized ``amount`` here needs no change.
- **Cost-per-output** attributes spend to the `project` set via `lucnt.task()`;
  spend with no project is surfaced under ``"(unattributed)"`` — itself a CFO signal.
- **Anomaly score** is the largest z-score among an agent's *recent* home-currency
  spends against its own baseline — a rolling "how out of pattern is it lately".
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
_UNATTRIBUTED = "(unattributed)"
_MONEY = Decimal("0.0001")  # ledger precision, Numeric(18, 4)
_SCORE = Decimal("0.0001")


@dataclass(frozen=True)
class SpendRecord:
    """The minimal projection of a committed transaction the intelligence needs.

    `amount`/`currency` are the entity-local figures (``local_amount`` /
    ``local_currency``) so cross-agent comparisons are currency-consistent.
    """

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
    """The agent's modal currency (ties broken by largest total spend).

    Callers guarantee ``records`` is non-empty.
    """
    totals: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    counts: Counter[str] = Counter()
    for r in records:
        counts[r.currency] += 1
        totals[r.currency] += r.amount
    # Most transactions wins; tie -> most money.
    return max(counts, key=lambda c: (counts[c], totals[c]))


def compute_fingerprint(records: Sequence[SpendRecord]) -> FingerprintFeatures:
    """Learn one agent's spend fingerprint from its transaction history.

    Amount statistics and the anomaly score use only the agent's home-currency
    spends (mixing currencies is meaningless); vendor, hour, and velocity
    features use every record. With fewer than ``_MIN_FINGERPRINT_SAMPLES``
    home-currency spends, ``enough_data`` is False and stats/score are omitted.
    """
    if not records:
        return FingerprintFeatures(
            enough_data=False,
            home_currency=None,
            total_records=0,
            amount_stats=None,
            top_vendors=[],
            active_hours={},
            daily_velocity=Decimal("0"),
            anomaly_score=Decimal("0"),
        )

    home = _home_currency(records)
    total = len(records)

    # Vendor shares (by count) over all records.
    vendor_counts = Counter(r.vendor for r in records)
    top_vendors = [
        VendorShare(
            vendor=vendor,
            count=count,
            share=_q_score(Decimal(count) / Decimal(total)),
        )
        for vendor, count in vendor_counts.most_common(_TOP_VENDORS)
    ]

    # Active hours + daily velocity over all records.
    active_hours: dict[int, int] = dict(
        Counter(r.transaction_date.hour for r in records)
    )
    dates = [r.transaction_date for r in records]
    span_days = max((max(dates) - min(dates)).days + 1, 1)
    daily_velocity = _q_score(Decimal(total) / Decimal(span_days))

    # Amount stats + anomaly score over home-currency records only.
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
        )

    stats = amount_stats(home_amounts, home)
    anomaly = _recent_anomaly_score(home_amounts, stats)
    return FingerprintFeatures(
        enough_data=True,
        home_currency=home,
        total_records=total,
        amount_stats=stats,
        top_vendors=top_vendors,
        active_hours=active_hours,
        daily_velocity=daily_velocity,
        anomaly_score=anomaly,
    )


def _recent_anomaly_score(
    ordered_amounts: Sequence[Decimal], stats: AmountStats
) -> Decimal:
    """Max z-score among the trailing ``_RECENT_WINDOW`` spends (0 if flat)."""
    if stats.stddev <= 0:
        return Decimal("0")
    recent = ordered_amounts[-_RECENT_WINDOW:]
    zs = [abs(a - stats.mean) / stats.stddev for a in recent]
    return _q_score(max(zs))


def score_anomaly(
    features: FingerprintFeatures,
    *,
    amount: Decimal,
    currency: str,
    vendor: str,
) -> AnomalyScore:
    """Score a candidate spend against a learned fingerprint.

    Returns a z-score only when the fingerprint has a same-currency baseline with
    non-zero spread; otherwise ``z_score`` is None. ``is_anomalous`` fires on a
    z at/above threshold **or** a never-before-seen vendor.
    """
    known_vendors = {v.vendor for v in features.top_vendors}
    unknown_vendor = vendor not in known_vendors
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
    """Cost per unit of work, grouped by ``project`` and currency, dearest first.

    Spend with no project is grouped under ``"(unattributed)"``. Token metrics are
    reported only when every record in the group carries a token count.
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
        total_tokens = (
            sum(m.tokens or 0 for m in members) if all_have_tokens else None
        )
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
    """Project month-end spend from the current run-rate and compare to the budget.

    Linear run-rate: ``mtd / day_of_month * days_in_month``. ``on_track`` is False
    only when a positive budget is set and the projection exceeds it.
    """
    day_of_month = now.day
    days_in_month = calendar.monthrange(now.year, now.month)[1]
    run_rate = month_to_date / Decimal(day_of_month)
    projected = _q_money(run_rate * Decimal(days_in_month))
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
