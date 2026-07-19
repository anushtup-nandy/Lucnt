"""Unit tests for lucnt.ledger: authorize() / capture() / spend().

All backend interaction is mocked via httpx.MockTransport — none of this
needs a live server, per sdk/PLAN.md §6 ("Unit: mock transport").
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from decimal import Decimal

import httpx
import pytest

from lucnt import config, ledger
from lucnt.client import Transport
from lucnt.context import task
from lucnt.exceptions import (
    BudgetExhausted,
    ConfigurationError,
    LucntError,
    PolicyBlocked,
)
from lucnt.models import BudgetStatus, Transaction, Verdict


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


def test_authorize_sends_the_resolved_request_and_returns_the_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/transactions/authorize"
        body = json.loads(request.content)
        assert Decimal(str(body.pop("amount"))) == Decimal("1.83")
        assert body == {
            "agent": "research",
            "entity": "in",
            "vendor": "openai",
            "currency": "USD",
            "reason": "test",
            "intent": None,
            "project": None,
            "model": None,
            "tokens": None,
        }
        return httpx.Response(
            200,
            json={"authorization_id": "auth_1", "status": "approved", "reason": None},
        )

    _stub(monkeypatch, handler)
    verdict = ledger.authorize(
        vendor="openai",
        amount=1.83,
        currency="USD",
        reason="test",
        agent="research",
        entity="in",
    )
    assert verdict.approved
    assert verdict.authorization_id == "auth_1"


def test_authorize_reports_blocked_verdict_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "authorization_id": "auth_2",
                "status": "blocked",
                "reason": "over monthly budget",
            },
        )

    _stub(monkeypatch, handler)
    verdict = ledger.authorize(
        vendor="openai",
        amount=999_999,
        currency="USD",
        reason="test",
        agent="research",
        entity="in",
    )
    assert not verdict.approved
    assert verdict.status == "blocked"
    assert verdict.reason == "over monthly budget"


def test_authorize_inherits_agent_and_entity_from_ambient_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["agent"] == "ops"
        assert body["entity"] == "sg"
        assert body["intent"] == "founder summit"
        return httpx.Response(
            200,
            json={"authorization_id": "auth_3", "status": "approved", "reason": None},
        )

    _stub(monkeypatch, handler)
    with task(agent="ops", entity="sg", intent="founder summit"):
        ledger.authorize(
            vendor="singapore_airlines", amount=1240, currency="SGD", reason="r"
        )


def test_authorize_explicit_agent_overrides_ambient_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["agent"] == "override"
        return httpx.Response(
            200,
            json={"authorization_id": "auth_4", "status": "approved", "reason": None},
        )

    _stub(monkeypatch, handler)
    with task(agent="ops", entity="sg"):
        ledger.authorize(
            vendor="v", amount=1, currency="USD", reason="r", agent="override"
        )


def test_authorize_without_agent_or_task_raises_configuration_error() -> None:
    with pytest.raises(ConfigurationError):
        ledger.authorize(vendor="v", amount=1, currency="USD", reason="r", entity="sg")


def test_authorize_without_entity_or_task_raises_configuration_error() -> None:
    with pytest.raises(ConfigurationError):
        ledger.authorize(vendor="v", amount=1, currency="USD", reason="r", agent="ops")


def test_capture_sends_the_authorization_id_and_returns_the_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/transactions/capture"
        body = json.loads(request.content)
        assert body == {
            "authorization_id": "auth_1",
            "source": "sdk",
            "invoice_url": "https://example.test/invoice/1",
        }
        return httpx.Response(
            200,
            json={
                "id": "tx_1",
                "authorization_id": "auth_1",
                "status": "booked",
                "vendor": "singapore_airlines",
                "amount": 1240,
                "currency": "SGD",
            },
        )

    _stub(monkeypatch, handler)
    verdict = Verdict(authorization_id="auth_1", status="approved", reason=None)
    tx = ledger.capture(verdict, invoice_url="https://example.test/invoice/1")
    assert tx.id == "tx_1"
    assert tx.approved


def test_capture_parses_journal_and_tax(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Phase 2 capture response deserializes journal lines + tax treatment."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "tx_2",
                "authorization_id": "auth_2",
                "status": "completed",
                "vendor": "openai",
                "amount": 100,
                "currency": "USD",
                "journal": [
                    {
                        "account_code": "6020",
                        "account_name": "AI & Compute",
                        "debit": 100,
                        "credit": 0,
                        "ledger": "in",
                        "currency": "INR",
                    },
                    {
                        "account_code": "1010",
                        "account_name": "Cash & Wallet",
                        "debit": 0,
                        "credit": 100,
                        "ledger": "in",
                        "currency": "INR",
                    },
                ],
                "tax": {
                    "tax_type": "GST_RCM",
                    "jurisdiction": "India",
                    "rate": 0.18,
                    "tax_amount": 18,
                    "is_reclaimable": True,
                    "reclaim_amount": 18,
                    "currency": "INR",
                    "legal_reference": "IGST Act 2017 s.5(3)",
                    "reverse_charge": True,
                    "self_invoice_generated": True,
                },
            },
        )

    _stub(monkeypatch, handler)
    verdict = Verdict(authorization_id="auth_2", status="approved", reason=None)
    tx = ledger.capture(verdict)
    assert len(tx.journal) == 2
    assert tx.journal[0].debit == Decimal("100")
    assert tx.tax is not None
    assert tx.tax.tax_type == "GST_RCM"
    assert tx.tax.reverse_charge is True
    assert tx.tax.tax_amount == Decimal("18")


def test_spend_captures_when_approved(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/transactions/authorize":
            return httpx.Response(
                200,
                json={
                    "authorization_id": "auth_5",
                    "status": "approved",
                    "reason": None,
                },
            )
        return httpx.Response(
            200,
            json={
                "id": "tx_5",
                "authorization_id": "auth_5",
                "status": "booked",
                "vendor": "openai",
                "amount": 1.83,
                "currency": "USD",
            },
        )

    _stub(monkeypatch, handler)
    result = ledger.spend(
        vendor="openai",
        amount=1.83,
        currency="USD",
        reason="test",
        agent="research",
        entity="in",
        model="gpt-5",
        tokens=128_000,
    )
    assert isinstance(result, Transaction)
    assert result.id == "tx_5"


def test_spend_returns_the_blocking_verdict_without_capturing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "authorization_id": "auth_6",
                "status": "blocked",
                "reason": "over budget",
            },
        )

    _stub(monkeypatch, handler)
    result = ledger.spend(
        vendor="openai",
        amount=999_999,
        currency="USD",
        reason="test",
        agent="research",
        entity="in",
    )
    assert isinstance(result, Verdict)
    assert not result.approved
    assert requested_paths == ["/transactions/authorize"]


def _blocked_response(
    code: str, reason: str
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "authorization_id": "auth_b",
                "status": "blocked",
                "code": code,
                "reason": reason,
            },
        )

    return handler


def test_strict_authorize_raises_policy_blocked_on_merchant_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub(monkeypatch, _blocked_response("merchant_blocked", "vendor blocklisted"))
    with pytest.raises(PolicyBlocked) as exc_info:
        ledger.authorize(
            vendor="coinbase",
            amount=10,
            currency="USD",
            reason="r",
            agent="ops",
            entity="sg",
            strict=True,
        )
    assert exc_info.value.code == "merchant_blocked"
    assert exc_info.value.status == "blocked"
    assert not isinstance(exc_info.value, BudgetExhausted)


def test_strict_authorize_raises_budget_exhausted_on_budget_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub(monkeypatch, _blocked_response("budget_exhausted", "over monthly budget"))
    with pytest.raises(BudgetExhausted) as exc_info:
        ledger.authorize(
            vendor="v",
            amount=999_999,
            currency="USD",
            reason="r",
            agent="ops",
            entity="sg",
            strict=True,
        )
    # BudgetExhausted subclasses PolicyBlocked, so the broader catch works too.
    assert isinstance(exc_info.value, PolicyBlocked)
    assert exc_info.value.code == "budget_exhausted"


def test_strict_spend_raises_and_never_captures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return _blocked_response("merchant_blocked", "blocked")(request)

    _stub(monkeypatch, handler)
    with pytest.raises(PolicyBlocked):
        ledger.spend(
            vendor="coinbase",
            amount=10,
            currency="USD",
            reason="r",
            agent="ops",
            entity="sg",
            strict=True,
        )
    assert paths == ["/transactions/authorize"]  # capture never attempted


def test_non_strict_blocked_verdict_still_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default (strict=False) returns the blocking verdict, code included."""
    _stub(monkeypatch, _blocked_response("velocity_exceeded", "too fast"))
    verdict = ledger.authorize(
        vendor="v", amount=1, currency="USD", reason="r", agent="ops", entity="sg"
    )
    assert not verdict.approved
    assert verdict.code == "velocity_exceeded"


def test_check_budget_returns_status_for_matching_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/budgets"
        return httpx.Response(
            200,
            json={
                "agents": [
                    {
                        "id": "a1",
                        "name": "Other",
                        "slug": "other",
                        "entity_id": "e1",
                        "monthly_budget": 100,
                        "spent": 10,
                        "spent_today": 0,
                        "remaining": 90,
                        "pct_used": 10.0,
                        "status": "ok",
                    },
                    {
                        "id": "a2",
                        "name": "Ops",
                        "slug": "ops",
                        "entity_id": "e1",
                        "monthly_budget": 1000,
                        "spent": 900,
                        "spent_today": 50,
                        "remaining": 100,
                        "pct_used": 90.0,
                        "status": "warning",
                    },
                ],
                "workflows": [],
            },
        )

    _stub(monkeypatch, handler)
    with task(agent="ops", entity="sg"):
        status = ledger.check_budget()
    assert isinstance(status, BudgetStatus)
    assert status.agent == "ops"
    assert status.remaining == Decimal("100")
    assert status.status == "warning"
    assert status.exhausted is False


def test_check_budget_raises_when_agent_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"agents": [], "workflows": []})

    _stub(monkeypatch, handler)
    with pytest.raises(LucntError):
        ledger.check_budget(agent="ghost")


def test_get_transport_is_reused_across_calls() -> None:
    first = ledger._get_transport()
    second = ledger._get_transport()
    assert first is second


def test_get_transport_rebuilds_after_reconfigure() -> None:
    first = ledger._get_transport()
    config.configure(api_key="sk_other", base_url="https://api.test")
    second = ledger._get_transport()
    assert first is not second
