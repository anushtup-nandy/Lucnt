"""Unit tests for lucnt.meter and the vendor adapters.

Fake vendor clients spoof their `__module__` so `resolve_adapter` detects them
(anthropic/openai) without importing either SDK; the backend is mocked via
httpx.MockTransport, so no live server or real API key is needed.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest

from lucnt import config, ledger, meter
from lucnt.adapters import resolve_adapter
from lucnt.adapters.anthropic import AnthropicAdapter
from lucnt.adapters.base import Usage
from lucnt.adapters.openai import OpenAIAdapter
from lucnt.client import Transport
from lucnt.exceptions import LucntError, PolicyBlocked
from lucnt.meter import meter as meter_fn


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


# --- Fake vendor clients (module-spoofed so adapters detect them) ---


class _FakeAnthropicMessages:
    def __init__(self, response: object) -> None:
        self._response = response
        self.calls: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        return self._response


class FakeAnthropic:
    def __init__(self, response: object) -> None:
        self.messages = _FakeAnthropicMessages(response)
        self.other_attr = "delegated"


FakeAnthropic.__module__ = "anthropic"


class _FakeOpenAICompletions:
    def __init__(self, response: object) -> None:
        self._response = response
        self.calls: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        return self._response


class FakeOpenAI:
    def __init__(self, response: object) -> None:
        self.chat = SimpleNamespace(completions=_FakeOpenAICompletions(response))


FakeOpenAI.__module__ = "openai"


def _capture_handler(
    captured: list[dict[str, object]],
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/transactions/authorize":
            return httpx.Response(
                200,
                json={"authorization_id": "auth_m", "status": "approved", "code": "ok"},
            )
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "tx_m",
                "authorization_id": "auth_m",
                "status": "completed",
                "vendor": "anthropic",
                "amount": 3.5,
                "currency": "USD",
            },
        )

    return handler


# --- Adapter units ---


def test_anthropic_adapter_prices_actual_usage() -> None:
    """opus-4-8 at $5/$25 per 1M: 200k in + 100k out = $3.50."""
    cost = AnthropicAdapter().cost(
        Usage(model="claude-opus-4-8", input_tokens=200_000, output_tokens=100_000)
    )
    assert cost == Decimal("3.5000")


def test_adapter_unknown_model_raises() -> None:
    unknown = Usage(model="gpt-does-not-exist", input_tokens=1, output_tokens=1)
    with pytest.raises(LucntError):
        OpenAIAdapter().cost(unknown)


def test_resolve_adapter_picks_by_module() -> None:
    assert isinstance(resolve_adapter(FakeAnthropic(None)), AnthropicAdapter)
    assert isinstance(resolve_adapter(FakeOpenAI(None)), OpenAIAdapter)
    assert resolve_adapter(object()) is None


# --- meter() behaviour ---


def test_meter_anthropic_authorizes_then_captures_actual_cost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict[str, object]] = []
    _stub(monkeypatch, _capture_handler(captured))

    response = SimpleNamespace(
        model="claude-opus-4-8",
        usage=SimpleNamespace(input_tokens=200_000, output_tokens=100_000),
    )
    client = FakeAnthropic(response)
    metered = meter_fn(client, agent="research", entity="in")

    result = metered.messages.create(
        model="claude-opus-4-8",
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=100,
    )
    assert result is response  # the real vendor response is returned unchanged
    assert len(client.messages.calls) == 1  # the real create was invoked once
    assert len(captured) == 1
    assert captured[0]["amount"] == "3.5000"  # settled actual cost, not the estimate
    assert captured[0]["model"] == "claude-opus-4-8"
    assert captured[0]["tokens"] == 300_000  # input + output


def test_meter_openai_wraps_chat_completions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict[str, object]] = []
    _stub(monkeypatch, _capture_handler(captured))

    response = SimpleNamespace(
        model="gpt-4o",
        usage=SimpleNamespace(prompt_tokens=1000, completion_tokens=500),
    )
    metered = meter_fn(FakeOpenAI(response), agent="research", entity="in")
    metered.chat.completions.create(
        model="gpt-4o", messages=[{"role": "user", "content": "hi"}]
    )

    # gpt-4o $2.50/$10 per 1M: 1000*2.5 + 500*10 = 7500 / 1e6 = 0.0075
    assert captured[0]["amount"] == "0.0075"
    assert captured[0]["tokens"] == 1500


def test_meter_blocked_call_raises_and_never_calls_create(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/transactions/authorize"  # capture never reached
        return httpx.Response(
            200,
            json={
                "authorization_id": "auth_b",
                "status": "blocked",
                "code": "budget_exhausted",
                "reason": "over budget",
            },
        )

    _stub(monkeypatch, handler)
    response = SimpleNamespace(
        model="claude-opus-4-8",
        usage=SimpleNamespace(input_tokens=1, output_tokens=1),
    )
    client = FakeAnthropic(response)
    metered = meter_fn(client, agent="research", entity="in")

    with pytest.raises(PolicyBlocked):
        metered.messages.create(
            model="claude-opus-4-8",
            messages=[{"role": "user", "content": "x"}],
            max_tokens=10,
        )
    assert client.messages.calls == []  # the vendor call was never made


def test_meter_delegates_unmetered_attributes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub(monkeypatch, _capture_handler([]))
    client = FakeAnthropic(None)
    metered = meter_fn(client, agent="research", entity="in")
    assert metered.other_attr == "delegated"  # passthrough to the real client


def test_meter_unknown_client_raises() -> None:
    with pytest.raises(LucntError):
        meter_fn(object(), agent="research", entity="in")


def test_meter_is_exported_at_package_root() -> None:
    assert meter is meter_fn
