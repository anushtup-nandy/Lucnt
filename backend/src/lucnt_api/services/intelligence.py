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
# Cap the reported z. Leave-one-out scoring (T2-4) divides by the spread of the
# *other* points; a perfectly flat baseline has zero spread, so a differing
# candidate would be infinitely anomalous. Cap it to a large-but-finite score so
# the outlier still fires (unlike the old population-σ score, which mathematically
# capped a lone outlier at √(n-1) and so could not fire below 8 samples).
_MAX_Z = Decimal("10")
# Floor the run-rate divisor so an early-month projection is not wild: on day 1 a
# single renewal must not extrapolate to ~30x the budget (T2-9).
_MIN_PROJECTION_DAYS = 7
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
    # The *full* set of vendors the agent has transacted with (not just the top
    # few) — the honest "known vendor" set for `score_anomaly` (T2-8). Defaulted so
    # historical/test constructions that predate the field still build.
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


def compute_fingerprint(
    records: Sequence[SpendRecord],
    *,
    home_currency: str | None = None,
    now: datetime | None = None,
) -> FingerprintFeatures:
    """Learn one agent's spend fingerprint from its transaction history.

    Amount statistics and the anomaly score use only the agent's home-currency
    spends (mixing currencies is meaningless); vendor, hour, and velocity
    features use every record. With fewer than ``_MIN_FINGERPRINT_SAMPLES``
    home-currency spends, ``enough_data`` is False and stats/score are omitted.

    ``home_currency``: pin the home currency (the entity's functional currency)
    rather than deriving the modal one, so it is stable month to month and never
    silently flips on a currency-count tie (T2-6). Falls back to the modal
    currency when not given.

    ``now``: age the daily velocity to the present, so a dormant agent's velocity
    decays instead of reading present-tense off its last-active span (T2-16).
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

    # Active hours + daily velocity over all records. The velocity span runs from
    # the first spend to `now` (if given) rather than to the last spend, so a
    # long-dormant agent reads a low current velocity, not its historical burst.
    active_hours: dict[int, int] = dict(
        Counter(r.transaction_date.hour for r in records)
    )
    dates = [r.transaction_date for r in records]
    span_end = now if now is not None else max(dates)
    span_days = max((span_end - min(dates)).days + 1, 1)
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
    """z-score of ``amounts[idx]`` against the mean/σ of every *other* amount.

    Leave-one-out (T2-4): the scored point is excluded from its own baseline, so a
    lone outlier cannot mute itself by dragging the mean/σ toward it (the bug that
    capped a lone outlier at √(n-1) and hid it below 8 samples). Returns None when
    there is no baseline; a differing candidate against a *flat* baseline (zero
    spread) is maximally anomalous, capped at ``_MAX_Z``.
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

    Returns a z-score only when the fingerprint has a same-currency baseline with
    non-zero spread; otherwise ``z_score`` is None. ``is_anomalous`` fires on a
    z at/above threshold **or** a never-before-seen vendor.

    "Never-before-seen" is judged against the agent's **full** vendor history
    (`features.known_vendors`), not just its top few — flagging every vendor
    outside the top 3 as anomalous was wrong (T2-8). This function is correct but
    **not yet wired** into a call site; wiring it into `authorize()` (with a
    share-floor to avoid alerting on established-but-diverse spenders) is P6.
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
    """Project month-end spend from the current run-rate and compare to the budget.

    Linear run-rate over a **floored** elapsed-day count:
    ``mtd / max(day_of_month, _MIN_PROJECTION_DAYS) * days_in_month``. The floor
    dampens the early-month explosion where a single day-1 renewal would otherwise
    extrapolate to ~30x itself and cry "overrun" every month (T2-9). The projection
    is never below the money already spent this month. ``now`` is expected in the
    entity's local timezone so month boundaries flip at local midnight, not 05:30
    for IST. ``on_track`` is False only when a positive budget is set and the
    projection exceeds it.
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
