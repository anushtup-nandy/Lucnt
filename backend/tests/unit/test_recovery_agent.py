"""Unit tests for the pure recovery agent (draft-only, no I/O)."""

from __future__ import annotations

from decimal import Decimal

from lucnt_api.models.reconciliation_exception import ReconExceptionType
from lucnt_api.services.recovery_agent import (
    METHOD_CANCEL_SUBSCRIPTION,
    METHOD_REFUND_REQUEST,
    METHOD_VENDOR_EMAIL,
    plan_recovery,
)


def _plan(exception_type: ReconExceptionType):  # type: ignore[no-untyped-def]
    """plan_recovery with fixed vendor/amount for assertions."""
    return plan_recovery(
        exception_type, vendor="acme", amount_at_stake=Decimal("12.50"), currency="USD"
    )


def test_duplicate_drafts_a_vendor_dispute_email() -> None:
    plan = _plan(ReconExceptionType.DUPLICATE)
    assert plan.method == METHOD_VENDOR_EMAIL
    assert plan.escalate_to_human is False
    assert "acme" in plan.message and "12.50 USD" in plan.message


def test_overcharge_drafts_a_vendor_dispute_email() -> None:
    plan = _plan(ReconExceptionType.OVERCHARGE)
    assert plan.method == METHOD_VENDOR_EMAIL
    assert plan.escalate_to_human is False


def test_unused_subscription_escalates_to_human() -> None:
    plan = _plan(ReconExceptionType.UNUSED_SUBSCRIPTION)
    assert plan.method == METHOD_CANCEL_SUBSCRIPTION
    assert plan.escalate_to_human is True


def test_failed_delivery_drafts_a_refund_request() -> None:
    plan = _plan(ReconExceptionType.FAILED_DELIVERY)
    assert plan.method == METHOD_REFUND_REQUEST
    assert plan.escalate_to_human is False


def test_plan_is_json_serializable() -> None:
    """as_dict() yields the fields persisted to evidence_json."""
    plan = _plan(ReconExceptionType.DUPLICATE)
    d = plan.as_dict()
    assert set(d) == {"method", "message", "escalate_to_human"}
