"""Insight & memory drafting (M8) — pure; the router persists the results.

Rules: budget_risk (run-rate overrun), anomaly (z ≥ threshold), cost_spike.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from lucnt_api.models.insight import InsightType
from lucnt_api.models.memory import MemoryType
from lucnt_api.services.intelligence import (
    _MIN_FINGERPRINT_SAMPLES,
    ANOMALY_Z_THRESHOLD,
    CostUnit,
    FingerprintFeatures,
    SpendProjection,
)

_UNATTRIBUTED = "(unattributed)"
_COST_SPIKE_FACTOR = Decimal("3")
_COST_SPIKE_MIN_UNITS = 3  # units needed in a currency to establish a baseline
_COST_SPIKE_MIN_CHARGES = 2  # a one-off charge is not a sustained cost spike

# Column-length guards: an over-long string raises DataError and rolls back the
# whole company's recompute.
_TITLE_MAX = 255
_VALUE_TEXT_MAX = 100
_ACTION_LABEL_MAX = 100
_SUMMARY_MAX = 255


def _clamp(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _dedup_key(
    insight_type: InsightType, agent_id: uuid.UUID | None, subject: str = ""
) -> str:
    """Recompute upsert key `type:agent:subject`; must match the migration back-fill."""
    return f"{insight_type.value}:{agent_id if agent_id is not None else ''}:{subject}"


@dataclass(frozen=True)
class AgentIntel:
    """Intelligence bundle for one agent — the generator's input."""

    agent_id: uuid.UUID
    agent_name: str
    agent_slug: str
    currency: str
    projection: SpendProjection
    features: FingerprintFeatures
    cost_units: list[CostUnit]


@dataclass(frozen=True)
class DraftInsight:
    """A drafted insight; the router maps it to an `insights` row."""

    insight_type: InsightType
    agent_id: uuid.UUID | None
    dedup_key: str
    title: str
    body: str
    value_text: str | None
    action_label: str | None
    action_payload: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Clamp string-column fields to length; `body` is TEXT, unbounded."""
        object.__setattr__(self, "title", _clamp(self.title, _TITLE_MAX))
        if self.value_text is not None:
            object.__setattr__(
                self, "value_text", _clamp(self.value_text, _VALUE_TEXT_MAX)
            )
        if self.action_label is not None:
            object.__setattr__(
                self, "action_label", _clamp(self.action_label, _ACTION_LABEL_MAX)
            )


@dataclass(frozen=True)
class DraftMemory:
    """A drafted per-agent memory; the router upserts it into `memories`."""

    agent_id: uuid.UUID
    memory_type: MemoryType
    summary: str
    detail_text: str
    metrics_json: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Clamp `summary` to length; `detail_text` is TEXT, unbounded."""
        object.__setattr__(self, "summary", _clamp(self.summary, _SUMMARY_MAX))


def _median(values: Sequence[Decimal]) -> Decimal:
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2 == 1:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


# --- Insights ---


def generate_insights(intels: Sequence[AgentIntel]) -> list[DraftInsight]:
    """Draft every supported insight, money-at-risk first."""
    return [
        *_budget_risk_insights(intels),
        *_anomaly_insights(intels),
        *_cost_spike_insights(intels),
    ]


def _budget_risk_insights(intels: Sequence[AgentIntel]) -> list[DraftInsight]:
    out: list[DraftInsight] = []
    for intel in intels:
        proj = intel.projection
        if proj.on_track or proj.projected_overrun <= 0:
            continue
        stake = f"{proj.projected_overrun} {intel.currency}"
        out.append(
            DraftInsight(
                insight_type=InsightType.BUDGET_RISK,
                agent_id=intel.agent_id,
                dedup_key=_dedup_key(InsightType.BUDGET_RISK, intel.agent_id),
                title=f"{intel.agent_name} is projected to overrun its budget",
                body=(
                    f"At the current run-rate {intel.agent_name} will spend "
                    f"~{proj.projected_month_end} {intel.currency} this month "
                    f"against a {proj.monthly_budget} {intel.currency} budget — "
                    f"a projected {stake} overrun (day {proj.day_of_month} of "
                    f"{proj.days_in_month})."
                ),
                value_text=f"{stake} projected overrun",
                action_label="Raise budget",
                action_payload={
                    "agent_slug": intel.agent_slug,
                    "suggested_budget": str(proj.projected_month_end),
                },
            )
        )
    return out


def _anomaly_insights(intels: Sequence[AgentIntel]) -> list[DraftInsight]:
    out: list[DraftInsight] = []
    for intel in intels:
        features = intel.features
        stats = features.amount_stats
        if not features.enough_data or stats is None or features.anomaly_score <= 0:
            continue
        if features.anomaly_score < ANOMALY_Z_THRESHOLD:
            continue
        out.append(
            DraftInsight(
                insight_type=InsightType.ANOMALY,
                agent_id=intel.agent_id,
                dedup_key=_dedup_key(InsightType.ANOMALY, intel.agent_id),
                title=f"{intel.agent_name} had an out-of-pattern spend",
                body=(
                    f"A recent {intel.agent_name} spend sits "
                    f"{features.anomaly_score}σ from its own baseline "
                    f"(mean {stats.mean} {stats.currency}, "
                    f"σ {stats.stddev}). Worth a quick review."
                ),
                value_text=f"{features.anomaly_score}σ deviation",
                action_label="Review agent",
                action_payload={"agent_slug": intel.agent_slug},
            )
        )
    return out


def _cost_spike_insights(intels: Sequence[AgentIntel]) -> list[DraftInsight]:
    by_currency: dict[str, list[tuple[AgentIntel, CostUnit]]] = defaultdict(list)
    for intel in intels:
        for unit in intel.cost_units:
            if unit.unit == _UNATTRIBUTED:
                continue
            if unit.count < _COST_SPIKE_MIN_CHARGES:
                continue
            by_currency[unit.currency].append((intel, unit))

    out: list[DraftInsight] = []
    for currency, pairs in by_currency.items():
        if len(pairs) < _COST_SPIKE_MIN_UNITS:
            continue
        costs = [u.avg_cost for _, u in pairs]
        for idx, (intel, unit) in enumerate(pairs):
            # Leave-one-out so the candidate can't inflate its own baseline.
            median_cost = _median(costs[:idx] + costs[idx + 1 :])
            if median_cost <= 0:
                continue
            threshold = median_cost * _COST_SPIKE_FACTOR
            if unit.avg_cost < threshold:
                continue
            out.append(
                DraftInsight(
                    insight_type=InsightType.COST_SPIKE,
                    agent_id=intel.agent_id,
                    dedup_key=_dedup_key(
                        InsightType.COST_SPIKE, intel.agent_id, unit.unit
                    ),
                    title=f"'{unit.unit}' costs far more than the typical unit",
                    body=(
                        f"{intel.agent_name}'s '{unit.unit}' averages "
                        f"{unit.avg_cost} {currency} per unit across {unit.count} "
                        f"charges — over {_COST_SPIKE_FACTOR}x the company median "
                        f"of {median_cost} {currency}."
                    ),
                    value_text=f"{unit.avg_cost} {currency}/unit",
                    action_label="Inspect project",
                    action_payload={
                        "agent_slug": intel.agent_slug,
                        "project": unit.unit,
                    },
                )
            )
    return out


# --- Memories ---


def derive_memories(intels: Sequence[AgentIntel]) -> list[DraftMemory]:
    """Draft each agent's durable learned patterns from its intelligence bundle."""
    out: list[DraftMemory] = []
    for intel in intels:
        out.extend(_agent_memories(intel))
    return out


def _agent_memories(intel: AgentIntel) -> list[DraftMemory]:
    out: list[DraftMemory] = []
    features = intel.features
    stats = features.amount_stats

    if stats is not None:
        out.append(
            DraftMemory(
                agent_id=intel.agent_id,
                memory_type=MemoryType.SPEND_PATTERN,
                summary=(
                    f"Typically spends ~{stats.median} {stats.currency} "
                    f"({stats.minimum}–{stats.maximum} range)"
                ),
                detail_text=(
                    f"Over {stats.sample_size} spends in {stats.currency}: mean "
                    f"{stats.mean}, σ {stats.stddev}, at "
                    f"~{features.daily_velocity} spends/day."
                ),
                metrics_json={
                    "currency": stats.currency,
                    "sample_size": stats.sample_size,
                    "median": str(stats.median),
                    "mean": str(stats.mean),
                    "stddev": str(stats.stddev),
                    "min": str(stats.minimum),
                    "max": str(stats.maximum),
                    "daily_velocity": str(features.daily_velocity),
                },
            )
        )
    elif features.total_records > 0:
        # Cold start: a "still learning" row instead of a blank Memory page.
        out.append(
            DraftMemory(
                agent_id=intel.agent_id,
                memory_type=MemoryType.SPEND_PATTERN,
                summary=(
                    f"Still learning {intel.agent_name}'s spend pattern "
                    f"({features.total_records}/{_MIN_FINGERPRINT_SAMPLES} spends)"
                ),
                detail_text=(
                    "Not enough same-currency history yet to establish a baseline; "
                    "stats and anomaly detection activate once there are at least "
                    f"{_MIN_FINGERPRINT_SAMPLES} spends in the home currency."
                ),
                metrics_json={
                    "learning": True,
                    "records_so_far": features.total_records,
                    "samples_needed": _MIN_FINGERPRINT_SAMPLES,
                },
            )
        )

    if features.top_vendors:
        top = features.top_vendors[0]
        pct = (top.share * 100).quantize(Decimal("0.1"))
        out.append(
            DraftMemory(
                agent_id=intel.agent_id,
                memory_type=MemoryType.VENDOR_AFFINITY,
                summary=f"Concentrates {pct}% of spend on {top.vendor}",
                detail_text=(
                    "Top vendors by transaction count: "
                    + ", ".join(f"{v.vendor} ({v.count})" for v in features.top_vendors)
                    + "."
                ),
                metrics_json={
                    "top_vendors": [
                        {"vendor": v.vendor, "count": v.count, "share": str(v.share)}
                        for v in features.top_vendors
                    ]
                },
            )
        )

    attributed = [u for u in intel.cost_units if u.unit != _UNATTRIBUTED]
    if attributed:
        dearest = attributed[0]  # cost_units arrive sorted by avg_cost desc
        out.append(
            DraftMemory(
                agent_id=intel.agent_id,
                memory_type=MemoryType.COST_PROFILE,
                summary=(
                    f"Priciest output: '{dearest.unit}' at "
                    f"{dearest.avg_cost} {dearest.currency}/unit"
                ),
                detail_text=(
                    f"'{dearest.unit}' cost {dearest.total_spent} "
                    f"{dearest.currency} across {dearest.count} charges "
                    f"({dearest.avg_cost} each)."
                    + (
                        f" ~{dearest.cost_per_1k_tokens} {dearest.currency}/1K tokens."
                        if dearest.cost_per_1k_tokens is not None
                        else ""
                    )
                ),
                metrics_json={
                    "unit": dearest.unit,
                    "currency": dearest.currency,
                    "avg_cost": str(dearest.avg_cost),
                    "total_spent": str(dearest.total_spent),
                    "count": dearest.count,
                    "cost_per_1k_tokens": (
                        str(dearest.cost_per_1k_tokens)
                        if dearest.cost_per_1k_tokens is not None
                        else None
                    ),
                },
            )
        )
    return out
