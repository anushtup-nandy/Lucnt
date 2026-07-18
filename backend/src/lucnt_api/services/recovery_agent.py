"""Recovery agent (Phase 4, M7) — maps an exception to a drafted recovery action.

Pure and draft-only (design decision, `logs/log_14070726.md`): given an exception
type and the transaction facts, it returns a `RecoveryPlan` (method + a drafted
message + a human-escalation flag). The router stores the plan in the exception's
`evidence_json` so the recovery inbox shows the recommended action immediately.

Actually *sending* the drafted message (vendor email / portal submission) is a
Phase 6 concern (webhooks / notifications); keeping this module side-effect-free
mirrors the pure `policy_engine` / `tax_engine` services and keeps it fully
unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from lucnt_api.models.reconciliation_exception import ReconExceptionType

# Recovery methods (business constants).
METHOD_VENDOR_EMAIL = "vendor_email"
METHOD_REFUND_REQUEST = "refund_request"
METHOD_CANCEL_SUBSCRIPTION = "cancel_subscription"


@dataclass(frozen=True)
class RecoveryPlan:
    """The recommended recovery action for one exception (stored in evidence_json)."""

    method: str
    message: str
    escalate_to_human: bool

    def as_dict(self) -> dict[str, object]:
        """JSON-serializable form for persistence in `evidence_json`."""
        return {
            "method": self.method,
            "message": self.message,
            "escalate_to_human": self.escalate_to_human,
        }


def plan_recovery(
    exception_type: ReconExceptionType,
    *,
    vendor: str,
    amount_at_stake: Decimal,
    currency: str,
) -> RecoveryPlan:
    """Draft the recovery action for an exception.

    Args:
        exception_type: Which anomaly this is.
        vendor: The vendor to recover from.
        amount_at_stake: The recoverable amount (in `currency`).
        currency: ISO currency code of `amount_at_stake`.

    Returns:
        A `RecoveryPlan` — the method, a drafted human-readable message, and
        whether it needs human escalation.
    """
    stake = f"{amount_at_stake} {currency}"
    if exception_type == ReconExceptionType.DUPLICATE:
        return RecoveryPlan(
            method=METHOD_VENDOR_EMAIL,
            message=(
                f"Dispute duplicate charge of {stake} from {vendor}: an identical "
                f"charge was already booked within 24 hours. Request a reversal."
            ),
            escalate_to_human=False,
        )
    if exception_type == ReconExceptionType.OVERCHARGE:
        return RecoveryPlan(
            method=METHOD_VENDOR_EMAIL,
            message=(
                f"Dispute overcharge from {vendor}: this charge is far above the "
                f"typical amount for this vendor. Request an adjustment of {stake}."
            ),
            escalate_to_human=False,
        )
    if exception_type == ReconExceptionType.UNUSED_SUBSCRIPTION:
        return RecoveryPlan(
            method=METHOD_CANCEL_SUBSCRIPTION,
            message=(
                f"Review recurring {vendor} subscription ({stake}/period) for "
                f"usage; if unused, cancel it and reclaim the latest charge."
            ),
            escalate_to_human=True,
        )
    if exception_type == ReconExceptionType.FAILED_DELIVERY:
        return RecoveryPlan(
            method=METHOD_REFUND_REQUEST,
            message=(
                f"Request refund of {stake} from {vendor}: the paid-for service "
                f"was not delivered (agent-reported task failure)."
            ),
            escalate_to_human=False,
        )
    raise AssertionError(f"unhandled exception type {exception_type!r}")
