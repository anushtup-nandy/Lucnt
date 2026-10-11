"""Reconciliation detectors (M7): pure anomaly detection over the ledger.

Overcharge compares actuals to actuals, not settled-vs-authorized (that would
false-positive on every metered spend). `failed_delivery` is agent-reported.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from lucnt_api.models.reconciliation_exception import ReconExceptionType

# --- Detector defaults (business constants) ---
# Tight window: a real double-charge lands within minutes; wider flags routine
# repeat top-ups and daily billing.
_DUPLICATE_WINDOW = timedelta(minutes=5)
_OVERCHARGE_FACTOR = Decimal("3")
# Baseline size excluding the candidate (leave-one-out).
_OVERCHARGE_MIN_SAMPLES = 3
_SUBSCRIPTION_MIN_OCCURRENCES = 3
_SUBSCRIPTION_CADENCE_DAYS = (25, 35)  # inclusive ~monthly window
_LEDGER_PRECISION = Decimal("0.0001")  # Numeric(18, 4)


@dataclass(frozen=True)
class TxnView:
    """The minimal projection of a committed transaction a detector needs."""

    id: uuid.UUID
    agent_id: uuid.UUID
    vendor: str
    paid_amount: Decimal
    paid_currency: str
    transaction_date: datetime  # tz-aware


@dataclass(frozen=True)
class DetectedException:
    """One detector hit; the router maps it to a `reconciliation_exceptions` row."""

    transaction_id: uuid.UUID
    exception_type: ReconExceptionType
    amount_at_stake: Decimal
    evidence: dict[str, object]  # JSON-serializable; persisted to evidence_json


# --- Helpers ---


def _amount_key(amount: Decimal) -> Decimal:
    return amount.quantize(_LEDGER_PRECISION)


def _vendor_key(vendor: str) -> str:
    return vendor.strip().lower()


def _median(values: Sequence[Decimal]) -> Decimal:
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2 == 1:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def recurring_identity(view: TxnView) -> str:
    """``agent:vendor:amount:currency`` — recon dedup key, same as detector grouping.

    Lets a dismissed subscription stay dismissed next month.
    """
    return (
        f"{view.agent_id}:{_vendor_key(view.vendor)}"
        f":{_amount_key(view.paid_amount)}:{view.paid_currency}"
    )


def _longest_monthly_run(gaps: Sequence[int], lo: int, hi: int) -> int:
    """Longest run of consecutive gaps inside ``[lo, hi]`` days."""
    best = run = 0
    for g in gaps:
        run = run + 1 if lo <= g <= hi else 0
        best = max(best, run)
    return best


_IdentityKey = tuple[uuid.UUID, str, Decimal, str]


def _group_by_identity(txns: Sequence[TxnView]) -> dict[_IdentityKey, list[TxnView]]:
    groups: dict[_IdentityKey, list[TxnView]] = defaultdict(list)
    for t in txns:
        key = (
            t.agent_id,
            _vendor_key(t.vendor),
            _amount_key(t.paid_amount),
            t.paid_currency,
        )
        groups[key].append(t)
    return groups


# --- Detectors ---


def detect_duplicates(
    txns: Sequence[TxnView], *, window: timedelta = _DUPLICATE_WINDOW
) -> list[DetectedException]:
    """Flag a charge identical to an earlier one within `window` (a double-charge)."""
    window_minutes = int(window.total_seconds() // 60)
    out: list[DetectedException] = []
    for members in _group_by_identity(txns).values():
        if len(members) < 2:
            continue
        ordered = sorted(members, key=lambda t: t.transaction_date)
        for i in range(1, len(ordered)):
            current = ordered[i]
            # Strict `<`: a gap of exactly the window is a cadence.
            original = next(
                (
                    prior
                    for prior in ordered[:i]
                    if current.transaction_date - prior.transaction_date < window
                ),
                None,
            )
            if original is None:
                continue
            out.append(
                DetectedException(
                    transaction_id=current.id,
                    exception_type=ReconExceptionType.DUPLICATE,
                    amount_at_stake=current.paid_amount,
                    evidence={
                        "original_transaction_id": str(original.id),
                        "original_date": original.transaction_date.isoformat(),
                        "duplicate_date": current.transaction_date.isoformat(),
                        "window_minutes": window_minutes,
                        "vendor": current.vendor,
                        "amount": str(current.paid_amount),
                        "currency": current.paid_currency,
                    },
                )
            )
    return out


def detect_overcharges(
    txns: Sequence[TxnView],
    *,
    factor: Decimal = _OVERCHARGE_FACTOR,
    min_samples: int = _OVERCHARGE_MIN_SAMPLES,
) -> list[DetectedException]:
    """Flag ``paid_amount >= factor * median`` of other same-currency charges.

    Grouped per (agent, vendor, currency); leave-one-out so an outlier can't
    inflate its own baseline.
    """
    groups: dict[tuple[uuid.UUID, str, str], list[TxnView]] = defaultdict(list)
    for t in txns:
        groups[(t.agent_id, _vendor_key(t.vendor), t.paid_currency)].append(t)

    out: list[DetectedException] = []
    for members in groups.values():
        if len(members) <= min_samples:
            continue
        for t in members:
            baseline = [m.paid_amount for m in members if m.id != t.id]
            median = _median(baseline)
            if median <= 0:
                continue
            threshold = median * factor
            if t.paid_amount >= threshold:
                out.append(
                    DetectedException(
                        transaction_id=t.id,
                        exception_type=ReconExceptionType.OVERCHARGE,
                        amount_at_stake=t.paid_amount - median,
                        evidence={
                            "median": str(median),
                            "factor": str(factor),
                            "threshold": str(threshold),
                            "baseline_size": len(baseline),
                            "vendor": t.vendor,
                            "amount": str(t.paid_amount),
                            "currency": t.paid_currency,
                        },
                    )
                )
    return out


def detect_unused_subscriptions(
    txns: Sequence[TxnView],
    *,
    min_occurrences: int = _SUBSCRIPTION_MIN_OCCURRENCES,
    cadence_days: tuple[int, int] = _SUBSCRIPTION_CADENCE_DAYS,
) -> list[DetectedException]:
    """Flag the latest of an identical charge recurring on a ~monthly cadence.

    Detects the pattern only; "unused" is confirmed by a human. Gaps are rounded
    (not truncated) to days and must form a consecutive monthly run.
    """
    lo, hi = cadence_days
    out: list[DetectedException] = []
    for members in _group_by_identity(txns).values():
        if len(members) < min_occurrences:
            continue
        ordered = sorted(members, key=lambda t: t.transaction_date)
        gaps = [
            round(
                (
                    ordered[i].transaction_date - ordered[i - 1].transaction_date
                ).total_seconds()
                / 86400
            )
            for i in range(1, len(ordered))
        ]
        if _longest_monthly_run(gaps, lo, hi) < min_occurrences - 1:
            continue
        latest = ordered[-1]
        out.append(
            DetectedException(
                transaction_id=latest.id,
                exception_type=ReconExceptionType.UNUSED_SUBSCRIPTION,
                amount_at_stake=latest.paid_amount,
                evidence={
                    "occurrences": len(ordered),
                    "cadence_days_observed": gaps,
                    "monthly_amount": str(latest.paid_amount),
                    "currency": latest.paid_currency,
                    "first_date": ordered[0].transaction_date.isoformat(),
                    "latest_date": latest.transaction_date.isoformat(),
                    "vendor": latest.vendor,
                },
            )
        )
    return out


def run_detectors(txns: Sequence[TxnView]) -> list[DetectedException]:
    """Run every ledger detector over `txns`."""
    return [
        *detect_duplicates(txns),
        *detect_overcharges(txns),
        *detect_unused_subscriptions(txns),
    ]
