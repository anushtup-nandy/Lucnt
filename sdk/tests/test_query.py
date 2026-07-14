"""Unit tests for lucnt.query (Phase 5 living financial memory).

Backend interaction is mocked via httpx.MockTransport — no live server needed.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from decimal import Decimal

import httpx
import pytest

from lucnt import config, context, ledger
from lucnt.client import Transport
from lucnt.exceptions import ConfigurationError, LucntError
from lucnt.models import FinancialMemory


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
    "agent": "research",
    "currency": "USD",
    "headline": "On track: projecting 900 USD against a 1000 USD budget.",
    "budget": {
        "monthly_budget": "1000",
        "spent": "500",
        "remaining": "500",
        "pct_used": "50.0",
        "status": "ok",
    },
    "projection": {
        "month_to_date": "300",
        "projected_month_end": "900",
        "monthly_budget": "1000",
        "on_track": True,
        "projected_overrun": "0",
        "day_of_month": 10,
        "days_in_month": 30,
    },
    "cost_per_output": [
        {
            "unit": "report",
            "dimension": "project",
            "currency": "USD",
            "total_spent": "20",
            "count": 2,
            "avg_cost": "10",
            "total_tokens": 2000,
            "cost_per_1k_tokens": "10",
        }
    ],
    "fingerprint": {
        "home_currency": "USD",
        "total_records": 4,
        "sample_size": 4,
        "mean_amount": "10",
        "stddev_amount": "0",
        "median_amount": "10",
        "daily_velocity": "1.0",
        "anomaly_score": "0",
        "top_vendors": [{"vendor": "openai", "count": 4, "share": "1.0"}],
    },
}


def test_query_parses_financial_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/intelligence/query"
        assert request.url.params["agent"] == "research"
        assert request.url.params["q"] == "am I on track?"
        return httpx.Response(200, json=_RESPONSE)

    _stub(monkeypatch, handler)
    mem = ledger.query(agent="research", question="am I on track?")
    assert isinstance(mem, FinancialMemory)
    assert mem.agent == "research"
    assert mem.on_track is True
    assert mem.budget.remaining == Decimal("500")
    assert mem.cost_per_output[0].avg_cost == Decimal("10")
    assert mem.cost_per_output[0].cost_per_1k_tokens == Decimal("10")
    assert mem.fingerprint is not None
    assert mem.fingerprint.top_vendors[0].vendor == "openai"


def test_query_omits_question_param_when_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["agent"] == "research"
        assert "q" not in request.url.params
        return httpx.Response(200, json=_RESPONSE)

    _stub(monkeypatch, handler)
    ledger.query(agent="research")


def test_query_uses_ambient_task_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["agent"] == "ambient"
        return httpx.Response(200, json={**_RESPONSE, "agent": "ambient"})

    _stub(monkeypatch, handler)
    with context.task(agent="ambient", entity="us"):
        mem = ledger.query()
    assert mem.agent == "ambient"


def test_query_requires_agent() -> None:
    with pytest.raises(ConfigurationError, match="'agent' is required"):
        ledger.query()


def test_query_maps_unknown_agent_to_lucnt_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "Unknown agent: ghost"})

    _stub(monkeypatch, handler)
    with pytest.raises(LucntError):
        ledger.query(agent="ghost")


def test_query_fingerprint_optional(monkeypatch: pytest.MonkeyPatch) -> None:
    blank = {**_RESPONSE, "fingerprint": None, "cost_per_output": []}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=blank)

    _stub(monkeypatch, handler)
    mem = ledger.query(agent="research")
    assert mem.fingerprint is None
    assert mem.cost_per_output == []


def test_query_is_exported() -> None:
    import lucnt

    assert hasattr(lucnt, "query")
    assert "query" in lucnt.__all__
    assert "FinancialMemory" in lucnt.__all__
