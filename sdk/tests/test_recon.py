"""Unit tests for lucnt.mark_failed_delivery."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from decimal import Decimal

import httpx
import pytest

from lucnt import config, ledger
from lucnt.client import Transport
from lucnt.exceptions import ConfigurationError
from lucnt.models import ReconException, Transaction


@pytest.fixture(autouse=True)
def _configured() -> Iterator[None]:
    config.configure(api_key="sk_test", base_url="https://api.test")
    yield
    config._reset_config()


@pytest.fixture(autouse=True)
def _reset_transport_cache() -> Iterator[None]:
    ledger._transport = None
    ledger._transport_config = None
    yield
    if ledger._transport is not None:
        ledger._transport.close()
    ledger._transport = None
    ledger._transport_config = None


def _stub(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> None:
    transport = Transport(config.get_config(), transport=httpx.MockTransport(handler))
    monkeypatch.setattr(ledger, "_get_transport", lambda: transport)


_RESPONSE = {
    "id": "recon_1",
    "transaction_id": "tx_1",
    "exception_type": "failed_delivery",
    "status": "open",
    "amount_at_stake": "50.00",
    "evidence": {"reason": "corrupt", "recovery_plan": {"method": "refund_request"}},
    "fix_notes": None,
    "created_at": "2026-07-14T12:00:00Z",  # server-only field, must be ignored
    "updated_at": "2026-07-14T12:00:00Z",
}


def test_mark_failed_delivery_with_transaction_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/recon/failed-delivery"
        assert json.loads(request.content) == {
            "transaction_id": "tx_1",
            "reason": "corrupt dataset",
        }
        return httpx.Response(201, json=_RESPONSE)

    _stub(monkeypatch, handler)
    tx = Transaction(
        id="tx_1",
        authorization_id="auth_1",
        status="completed",
        vendor="dataco",
        amount=Decimal("50.00"),
    )
    exc = ledger.mark_failed_delivery(transaction=tx, reason="corrupt dataset")
    assert isinstance(exc, ReconException)
    assert exc.exception_type == "failed_delivery"
    assert exc.amount_at_stake == Decimal("50.00")
    assert exc.evidence["recovery_plan"]["method"] == "refund_request"


def test_mark_failed_delivery_with_transaction_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content) == {
            "transaction_id": "tx_1",
            "reason": "no delivery",
        }
        return httpx.Response(201, json=_RESPONSE)

    _stub(monkeypatch, handler)
    exc = ledger.mark_failed_delivery(transaction_id="tx_1", reason="no delivery")
    assert exc.id == "recon_1"


def test_mark_failed_delivery_forwards_amount_at_stake(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["amount_at_stake"] == "20"  # Decimal -> str, exact
        return httpx.Response(201, json=_RESPONSE)

    _stub(monkeypatch, handler)
    ledger.mark_failed_delivery(
        transaction_id="tx_1", reason="partial", amount_at_stake=20
    )


def test_mark_failed_delivery_requires_a_transaction_reference() -> None:
    with pytest.raises(ConfigurationError, match="requires 'transaction'"):
        ledger.mark_failed_delivery(reason="x")


def test_mark_failed_delivery_is_exported() -> None:
    import lucnt

    assert hasattr(lucnt, "mark_failed_delivery")
    assert "ReconException" in lucnt.__all__
