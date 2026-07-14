"""Reconciliation detectors (Phase 4, M7) — pure anomaly detection over the ledger.

Mirrors `services/policy_engine.py` / `services/tax_engine.py`: the detectors are
pure functions over a lightweight `TxnView` value object, so this module imports
no SQLAlchemy and is unit-testable without a database. The router
(`routers/recon.py`) loads committed transactions, projects them to `TxnView`s,
runs `run_detectors`, and persists the results as `reconciliation_exceptions`.

Signals are limited to what the ledger can honestly support:

- **duplicate** — the same `(agent, vendor, amount, currency)` within a short
  window (a double-charge).
- **overcharge** — a charge that is a statistical outlier versus that
  `(agent, vendor)`'s own history (``>= factor * median``). Deliberately *not*
  settled-over-authorized, which would false-positive on every metered LLM spend
  (``authorize`` books a crude estimate; ``capture`` books the larger actual).
  Comparing actuals to actuals avoids that.
- **unused_subscription** — an identical charge recurring on a ~monthly cadence.
  With no per-subscription usage telemetry we surface the *pattern*; a human
  confirms disuse at resolve time.

`failed_delivery` is intentionally absent: it cannot be inferred from spend data
and is agent-reported through the SDK ``mark_failed_delivery()`` hook.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from lucnt.models.reconciliation_exception import ReconExceptionType

# --- Detector defaults (business constants) ---
_DUPLICATE_WINDOW = timedelta(hours=24)
_OVERCHARGE_FACTOR = Decimal("3")
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
    """Quantize to ledger precision so equal amounts group exactly."""
    return amount.quantize(_LEDGER_PRECISION)


def _vendor_key(vendor: str) -> str:
    """Case-insensitive, whitespace-trimmed vendor key for grouping."""
    return vendor.strip().lower()


def _median(values: Sequence[Decimal]) -> Decimal:
    """Median of a non-empty sequence, Decimal-exact (no float coercion)."""
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2 == 1:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


_IdentityKey = tuple[uuid.UUID, str, Decimal, str]


def _group_by_identity(txns: Sequence[TxnView]) -> dict[_IdentityKey, list[TxnView]]:
    """Group by (agent, vendor, quantized amount, currency) — an identical charge."""
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
    """Flag a charge identical to an earlier one within `window` (a double-charge).

    The earliest same-identity charge inside the window is treated as the
    original; every later one within the window is flagged, with the full amount
    recoverable.
    """
    out: list[DetectedException] = []
    for members in _group_by_identity(txns).values():
        if len(members) < 2:
            continue
        ordered = sorted(members, key=lambda t: t.transaction_date)
        for i in range(1, len(ordered)):
            current = ordered[i]
            original = next(
                (
                    prior
                    for prior in ordered[:i]
                    if current.transaction_date - prior.transaction_date <= window
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
                        "window_hours": window.total_seconds() / 3600,
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
    """Flag a charge that is a statistical outlier vs the (agent, vendor) history.

    A charge is flagged when ``paid_amount >= factor * median`` of that agent's
    charges to that vendor, given at least `min_samples` charges to establish a
    baseline. The median is robust to a lone outlier, and an identical recurring
    charge (a subscription) has median == amount, so it is never flagged here.
    """
    groups: dict[tuple[uuid.UUID, str], list[TxnView]] = defaultdict(list)
    for t in txns:
        groups[(t.agent_id, _vendor_key(t.vendor))].append(t)

    out: list[DetectedException] = []
    for members in groups.values():
        if len(members) < min_samples:
            continue
        median = _median([m.paid_amount for m in members])
        if median <= 0:
            continue
        threshold = median * factor
        for t in members:
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
                            "sample_size": len(members),
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

    Detects the recurring *pattern* only — 'unused' is a human judgement at
    resolve time (there is no per-subscription usage telemetry). Requires at
    least `min_occurrences` charges with at least ``min_occurrences - 1`` gaps
    inside the monthly `cadence_days` window.
    """
    lo, hi = cadence_days
    out: list[DetectedException] = []
    for members in _group_by_identity(txns).values():
        if len(members) < min_occurrences:
            continue
        ordered = sorted(members, key=lambda t: t.transaction_date)
        gaps = [
            (ordered[i].transaction_date - ordered[i - 1].transaction_date).days
            for i in range(1, len(ordered))
        ]
        monthly_gaps = [g for g in gaps if lo <= g <= hi]
        if len(monthly_gaps) < min_occurrences - 1:
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
    """Run every ledger detector over `txns` and return the combined hits."""
    return [
        *detect_duplicates(txns),
        *detect_overcharges(txns),
        *detect_unused_subscriptions(txns),
    ]
