"""Unit tests for the pure insight/memory generator (Phase 5, M8)."""

from __future__ import annotations

import uuid
from decimal import Decimal

from lucnt_api.models.insight import InsightType
from lucnt_api.models.memory import MemoryType
from lucnt_api.services.insight_generator import (
    AgentIntel,
    derive_memories,
    generate_insights,
)
from lucnt_api.services.intelligence import (
    AmountStats,
    CostUnit,
    FingerprintFeatures,
    SpendProjection,
    VendorShare,
)


def _projection(*, on_track: bool, overrun: str = "0") -> SpendProjection:
    return SpendProjection(
        month_to_date=Decimal("300"),
        projected_month_end=Decimal("900"),
        monthly_budget=Decimal("600"),
        on_track=on_track,
        projected_overrun=Decimal(overrun),
        day_of_month=10,
        days_in_month=30,
    )


def _features(
    *, anomaly_score: str = "1.0", enough: bool = True
) -> FingerprintFeatures:
    stats = (
        AmountStats(
            currency="USD",
            sample_size=5,
            mean=Decimal("18"),
            stddev=Decimal("16"),
            median=Decimal("10"),
            minimum=Decimal("10"),
            maximum=Decimal("50"),
        )
        if enough
        else None
    )
    return FingerprintFeatures(
        enough_data=enough,
        home_currency="USD",
        total_records=5,
        amount_stats=stats,
        top_vendors=[VendorShare(vendor="aws", count=3, share=Decimal("0.6"))],
        active_hours={12: 5},
        daily_velocity=Decimal("1.0"),
        anomaly_score=Decimal(anomaly_score),
    )


def _cost_unit(unit: str, avg: str, *, count: int = 2) -> CostUnit:
    # count defaults to 2: a cost spike needs a repeated charge, not a one-off.
    # total_spent is avg * count to stay internally consistent.
    return CostUnit(
        unit=unit,
        dimension="project",
        currency="USD",
        total_spent=Decimal(avg) * count,
        count=count,
        avg_cost=Decimal(avg),
        total_tokens=None,
        cost_per_1k_tokens=None,
    )


def _intel(
    *,
    slug: str = "ops",
    projection: SpendProjection | None = None,
    features: FingerprintFeatures | None = None,
    cost_units: list[CostUnit] | None = None,
) -> AgentIntel:
    return AgentIntel(
        agent_id=uuid.uuid4(),
        agent_name=f"Agent {slug}",
        agent_slug=slug,
        currency="USD",
        projection=projection or _projection(on_track=True),
        features=features or _features(),
        cost_units=cost_units or [],
    )


# --- insights ---


def test_budget_risk_fires_when_projected_over() -> None:
    intel = _intel(projection=_projection(on_track=False, overrun="300"))
    insights = generate_insights([intel])
    risk = [i for i in insights if i.insight_type == InsightType.BUDGET_RISK]
    assert len(risk) == 1
    assert risk[0].agent_id == intel.agent_id
    assert risk[0].value_text == "300 USD projected overrun"
    assert risk[0].action_payload["suggested_budget"] == "900"


def test_no_budget_risk_when_on_track() -> None:
    insights = generate_insights([_intel(projection=_projection(on_track=True))])
    assert not [i for i in insights if i.insight_type == InsightType.BUDGET_RISK]


def test_anomaly_fires_at_threshold() -> None:
    intel = _intel(features=_features(anomaly_score="3.0"))
    anomalies = [
        i for i in generate_insights([intel]) if i.insight_type == InsightType.ANOMALY
    ]
    assert len(anomalies) == 1
    assert "3.0" in anomalies[0].value_text  # e.g. "3.0σ deviation"


def test_no_anomaly_below_threshold() -> None:
    intel = _intel(features=_features(anomaly_score="2.0"))
    assert not [
        i for i in generate_insights([intel]) if i.insight_type == InsightType.ANOMALY
    ]


def test_cost_spike_fires_for_outlier_unit() -> None:
    # median of [10, 12, 100] is 12; threshold 3x = 36; only leadgen (100) spikes.
    intel = _intel(
        cost_units=[
            _cost_unit("report", "10"),
            _cost_unit("infra", "12"),
            _cost_unit("leadgen", "100"),
        ]
    )
    spikes = [
        i
        for i in generate_insights([intel])
        if i.insight_type == InsightType.COST_SPIKE
    ]
    assert len(spikes) == 1
    assert spikes[0].action_payload["project"] == "leadgen"


def test_cost_spike_needs_baseline() -> None:
    # < 3 units -> no baseline -> no spike even if amounts differ wildly.
    intel = _intel(cost_units=[_cost_unit("a", "10"), _cost_unit("b", "100")])
    assert not [
        i
        for i in generate_insights([intel])
        if i.insight_type == InsightType.COST_SPIKE
    ]


def test_cost_spike_ignores_unattributed() -> None:
    intel = _intel(
        cost_units=[
            _cost_unit("report", "10"),
            _cost_unit("infra", "12"),
            _cost_unit("(unattributed)", "100"),
        ]
    )
    assert not [
        i
        for i in generate_insights([intel])
        if i.insight_type == InsightType.COST_SPIKE
    ]


# --- memories ---


def test_derive_memories_full_agent() -> None:
    intel = _intel(cost_units=[_cost_unit("report", "10")])
    memories = derive_memories([intel])
    types = {m.memory_type for m in memories}
    assert types == {
        MemoryType.SPEND_PATTERN,
        MemoryType.VENDOR_AFFINITY,
        MemoryType.COST_PROFILE,
    }


def test_derive_memories_cold_start_row_when_learning() -> None:
    # Has spend but no baseline yet -> a single "still learning" cold-start row so a
    # new agent is not blank on the Memory page.
    bare = FingerprintFeatures(
        enough_data=False,
        home_currency="USD",
        total_records=1,
        amount_stats=None,
        top_vendors=[],
        active_hours={},
        daily_velocity=Decimal("0"),
        anomaly_score=Decimal("0"),
    )
    intel = _intel(features=bare, cost_units=[_cost_unit("(unattributed)", "5")])
    memories = derive_memories([intel])
    assert len(memories) == 1
    assert memories[0].memory_type == MemoryType.SPEND_PATTERN
    assert memories[0].metrics_json["learning"] is True


def test_derive_memories_empty_when_no_spend() -> None:
    # Zero records -> nothing to say at all.
    bare = FingerprintFeatures(
        enough_data=False,
        home_currency=None,
        total_records=0,
        amount_stats=None,
        top_vendors=[],
        active_hours={},
        daily_velocity=Decimal("0"),
        anomaly_score=Decimal("0"),
    )
    intel = _intel(features=bare, cost_units=[])
    assert derive_memories([intel]) == []


def test_vendor_affinity_summary_has_share_pct() -> None:
    memories = derive_memories([_intel()])
    affinity = next(m for m in memories if m.memory_type == MemoryType.VENDOR_AFFINITY)
    assert "60.0%" in affinity.summary
    assert "aws" in affinity.summary
