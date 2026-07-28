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


# --- T5-2: dated model snapshots resolve to their base alias ------------------


@pytest.mark.parametrize(
    ("reported", "base"),
    [
        ("gpt-4o-2024-08-06", "gpt-4o"),
        ("gpt-4o-mini-2024-07-18", "gpt-4o-mini"),
        ("gpt-3.5-turbo-0125", "gpt-3.5-turbo"),
    ],
)
def test_openai_dated_snapshot_prices_as_its_base(reported: str, base: str) -> None:
    """The T5-2 bug: response.model is a dated id, the pack keys the alias, so
    the first metered call used to be paid, unbooked, and its response lost."""
    adapter = OpenAIAdapter()
    usage = Usage(model=reported, input_tokens=1_000_000, output_tokens=0)
    assert adapter.cost(usage) == adapter.cost(
        Usage(model=base, input_tokens=1_000_000, output_tokens=0)
    )


def test_anthropic_dated_snapshot_prices_as_its_base() -> None:
    adapter = AnthropicAdapter()
    usage = Usage(
        model="claude-haiku-4-5-20251001", input_tokens=1_000_000, output_tokens=0
    )
    assert adapter.cost(usage) == Decimal("1.0000")


def test_longest_matching_base_wins() -> None:
    """gpt-4o-mini-2024-07-18 must price as gpt-4o-mini ($0.15), never as the
    5x-more-expensive gpt-4o ($2.50) that also prefixes it."""
    adapter = OpenAIAdapter()
    usage = Usage(
        model="gpt-4o-mini-2024-07-18", input_tokens=1_000_000, output_tokens=0
    )
    assert adapter.cost(usage) == Decimal("0.1500")


@pytest.mark.parametrize(
    "reported",
    ["gpt-4o-turbo-ultra", "gpt-4o-preview", "claude-opus-5-experimental"],
)
def test_a_non_snapshot_suffix_does_not_silently_borrow_a_price(
    reported: str,
) -> None:
    """The dangerous half of prefix matching: a genuinely new (and possibly
    pricier) variant must fail loudly rather than bill at the base's rate."""
    adapter = OpenAIAdapter() if reported.startswith("gpt") else AnthropicAdapter()
    with pytest.raises(LucntError, match="no pricing configured"):
        adapter.cost(Usage(model=reported, input_tokens=1, output_tokens=1))


def test_unpriced_reported_model_falls_back_to_the_requested_model() -> None:
    """T5-2's second clause: the vendor answered with an id we can't price, but
    the id the caller asked for is priced fine."""
    adapter = OpenAIAdapter()
    usage = Usage(
        model="gpt-4o-internal-alias", input_tokens=1_000_000, output_tokens=0
    )
    assert adapter.cost(usage, fallback_model="gpt-4o") == Decimal("2.5000")


def test_fallback_model_that_is_also_unpriced_still_raises() -> None:
    adapter = OpenAIAdapter()
    usage = Usage(model="mystery-a", input_tokens=1, output_tokens=1)
    with pytest.raises(LucntError, match="no pricing configured"):
        adapter.cost(usage, fallback_model="mystery-b")


def test_meter_books_a_dated_snapshot_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict[str, object]] = []
    _stub(monkeypatch, _capture_handler(captured))
    response = SimpleNamespace(
        model="gpt-4o-2024-08-06",
        usage=SimpleNamespace(prompt_tokens=1000, completion_tokens=500),
    )
    metered = meter_fn(FakeOpenAI(response), agent="research", entity="in")
    metered.chat.completions.create(
        model="gpt-4o", messages=[{"role": "user", "content": "hi"}]
    )
    assert captured[0]["amount"] == "0.0075"  # priced as gpt-4o
    assert captured[0]["model"] == "gpt-4o-2024-08-06"  # reported id is preserved


# --- T5-1: post-call failure must never destroy a paid response ---------------


def _unreadable_response() -> SimpleNamespace:
    """A response whose usage cannot be read — vendor contract drift.

    This, not an unpriced model, is what still reaches T5-1's fallback: the
    requested model must already be priced for `authorize()` to have run at all,
    and T5-2 falls back to that price, so pricing effectively cannot fail here.
    """
    return SimpleNamespace(model="claude-opus-5")  # no .usage attribute


def test_unreadable_usage_still_returns_the_paid_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The T5-1 headline: the customer paid the vendor, so they get their
    completion even though lucnt could not settle it."""
    captured: list[dict[str, object]] = []
    _stub(monkeypatch, _capture_handler(captured))
    response = _unreadable_response()
    metered = meter_fn(FakeAnthropic(response), agent="research", entity="in")

    result = metered.messages.create(
        model="claude-opus-5", messages=[{"role": "user", "content": "hi"}]
    )
    assert result is response


def test_unreadable_usage_books_the_authorized_estimate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Degradation step 2: losing the spend entirely would be worse than booking
    the hold's estimate, because attribution at the moment of spend is the
    product (M1)."""
    captured: list[dict[str, object]] = []
    _stub(monkeypatch, _capture_handler(captured))
    metered = meter_fn(FakeAnthropic(_unreadable_response()), agent="a", entity="e")

    metered.messages.create(
        model="claude-opus-5", messages=[{"role": "user", "content": "hi"}]
    )
    assert len(captured) == 1
    # No settled amount/model/tokens — the authorization's held values stand.
    assert "amount" not in captured[0]
    assert captured[0]["authorization_id"] == "auth_m"


def test_an_unpriced_reported_model_never_reaches_the_estimate_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T5-2 closes T5-1's most likely trigger: the vendor reporting an id the
    pack doesn't know now prices off the requested model and settles normally,
    rather than degrading to the hold's estimate."""
    captured: list[dict[str, object]] = []
    _stub(monkeypatch, _capture_handler(captured))
    response = SimpleNamespace(
        model="claude-some-unknown-alias",
        usage=SimpleNamespace(input_tokens=1_000_000, output_tokens=0),
    )
    metered = meter_fn(FakeAnthropic(response), agent="a", entity="e")
    metered.messages.create(
        model="claude-opus-5", messages=[{"role": "user", "content": "hi"}]
    )
    assert captured[0]["amount"] == "5.0000"  # settled at claude-opus-5's rate


def test_malformed_vendor_response_still_returns_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A vendor changing its response shape is a contract drift, not a reason to
    throw away a completion the customer paid for."""
    _stub(monkeypatch, _capture_handler([]))
    response = SimpleNamespace(model="claude-opus-5")  # no .usage at all
    metered = meter_fn(FakeAnthropic(response), agent="a", entity="e")
    assert (
        metered.messages.create(
            model="claude-opus-5", messages=[{"role": "user", "content": "hi"}]
        )
        is response
    )


def test_backend_failure_during_capture_still_returns_the_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both capture attempts fail; the hold is orphaned and logged, but the
    caller still gets what they paid for."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/transactions/authorize":
            return httpx.Response(
                200,
                json={"authorization_id": "auth_m", "status": "approved", "code": "ok"},
            )
        return httpx.Response(500, text="backend down")

    _stub(monkeypatch, handler)
    response = SimpleNamespace(
        model="claude-opus-5",
        usage=SimpleNamespace(input_tokens=10, output_tokens=5),
    )
    metered = meter_fn(FakeAnthropic(response), agent="a", entity="e")
    assert (
        metered.messages.create(
            model="claude-opus-5", messages=[{"role": "user", "content": "hi"}]
        )
        is response
    )


def test_booking_failure_is_logged_never_silenced(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _stub(monkeypatch, _capture_handler([]))
    metered = meter_fn(FakeAnthropic(_unreadable_response()), agent="a", entity="e")
    with caplog.at_level("ERROR", logger="lucnt.meter"):
        metered.messages.create(
            model="claude-opus-5", messages=[{"role": "user", "content": "hi"}]
        )
    assert "could not settle metered" in caplog.text
    assert "auth_m" in caplog.text  # the id needed to reconcile the hold


def test_orphaned_hold_is_logged_with_its_authorization_id(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/transactions/authorize":
            return httpx.Response(
                200,
                json={"authorization_id": "auth_orphan", "status": "approved"},
            )
        return httpx.Response(500, text="down")

    _stub(monkeypatch, handler)
    response = SimpleNamespace(
        model="claude-opus-5", usage=SimpleNamespace(input_tokens=1, output_tokens=1)
    )
    metered = meter_fn(FakeAnthropic(response), agent="a", entity="e")
    with caplog.at_level("ERROR", logger="lucnt.meter"):
        metered.messages.create(
            model="claude-opus-5", messages=[{"role": "user", "content": "hi"}]
        )
    assert "UNBOOKED" in caplog.text
    assert "auth_orphan" in caplog.text


def test_a_policy_block_still_raises_after_t5_1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T5-1 must not have widened into swallowing pre-call failures — blocking
    before money moves is the whole point of the gate."""
    _stub(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            json={
                "authorization_id": "auth_b",
                "status": "blocked",
                "code": "merchant_blocked",
            },
        ),
    )
    client = FakeAnthropic(_unreadable_response())
    metered = meter_fn(client, agent="a", entity="e")
    with pytest.raises(PolicyBlocked):
        metered.messages.create(
            model="claude-opus-5", messages=[{"role": "user", "content": "hi"}]
        )
    assert client.messages.calls == []


def test_keyboard_interrupt_is_not_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exception, not BaseException: a Ctrl-C during bookkeeping must propagate."""
    _stub(monkeypatch, _capture_handler([]))

    def boom(_self: object, _response: object) -> Usage:
        raise KeyboardInterrupt

    monkeypatch.setattr(AnthropicAdapter, "usage_from_response", boom)
    metered = meter_fn(FakeAnthropic(_unreadable_response()), agent="a", entity="e")
    with pytest.raises(KeyboardInterrupt):
        metered.messages.create(
            model="claude-opus-5", messages=[{"role": "user", "content": "hi"}]
        )


def test_meter_pricing_override_unblocks_an_unknown_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The T5-12 escape hatch closing the T5-10 hard-block, end to end."""
    captured: list[dict[str, object]] = []
    _stub(monkeypatch, _capture_handler(captured))
    response = SimpleNamespace(
        model="claude-unreleased-9",
        usage=SimpleNamespace(input_tokens=1_000_000, output_tokens=0),
    )
    metered = meter_fn(
        FakeAnthropic(response),
        agent="a",
        entity="e",
        pricing={"claude-unreleased-9": ("7", "35")},
    )
    metered.messages.create(
        model="claude-unreleased-9", messages=[{"role": "user", "content": "hi"}]
    )
    assert captured[0]["amount"] == "7.0000"


# --- T5-4: a failed vendor call releases its hold ------------------------------


class _FailingAnthropicMessages:
    def __init__(self, error: BaseException) -> None:
        self._error = error
        self.calls: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        raise self._error


class FailingAnthropic:
    def __init__(self, error: BaseException) -> None:
        self.messages = _FailingAnthropicMessages(error)


FailingAnthropic.__module__ = "anthropic"


def _void_tracking_handler(
    voids: list[dict[str, object]],
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/transactions/authorize":
            return httpx.Response(
                200,
                json={"authorization_id": "auth_v", "status": "approved", "code": "ok"},
            )
        if request.url.path == "/transactions/void":
            voids.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "authorization_id": "auth_v",
                    "released_at": "2026-07-28T12:00:00Z",
                    "already_released": False,
                },
            )
        raise AssertionError(f"unexpected path {request.url.path}")

    return handler


def test_a_failed_vendor_call_releases_its_hold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without this the hold reserves the agent's budget forever, so every vendor
    rate-limit or timeout permanently shrinks what the agent may spend (T5-4)."""
    voids: list[dict[str, object]] = []
    _stub(monkeypatch, _void_tracking_handler(voids))
    metered = meter_fn(
        FailingAnthropic(RuntimeError("vendor exploded")), agent="a", entity="e"
    )

    with pytest.raises(RuntimeError, match="vendor exploded"):
        metered.messages.create(
            model="claude-opus-5", messages=[{"role": "user", "content": "hi"}]
        )
    assert len(voids) == 1
    assert voids[0]["authorization_id"] == "auth_v"
    assert "RuntimeError" in str(voids[0]["reason"])


def test_the_vendors_own_error_is_what_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The caller needs the vendor's error, not whatever happened while lucnt was
    cleaning up after it."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/transactions/authorize":
            return httpx.Response(
                200,
                json={"authorization_id": "auth_v", "status": "approved"},
            )
        return httpx.Response(500, text="void endpoint is down too")

    _stub(monkeypatch, handler)
    sentinel = RuntimeError("the error the caller must see")
    metered = meter_fn(FailingAnthropic(sentinel), agent="a", entity="e")
    with pytest.raises(RuntimeError) as excinfo:
        metered.messages.create(
            model="claude-opus-5", messages=[{"role": "user", "content": "hi"}]
        )
    assert excinfo.value is sentinel


def test_a_failed_release_is_logged_and_defers_to_the_server_ttl(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/transactions/authorize":
            return httpx.Response(
                200, json={"authorization_id": "auth_v", "status": "approved"}
            )
        return httpx.Response(500, text="down")

    _stub(monkeypatch, handler)
    metered = meter_fn(FailingAnthropic(RuntimeError("boom")), agent="a", entity="e")
    with caplog.at_level("WARNING", logger="lucnt.meter"), pytest.raises(RuntimeError):
        metered.messages.create(
            model="claude-opus-5", messages=[{"role": "user", "content": "hi"}]
        )
    assert "could not release the hold" in caplog.text
    assert "TTL" in caplog.text


def test_a_keyboard_interrupt_mid_vendor_call_still_releases_the_hold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BaseException, not Exception, on this path: a Ctrl-C during the vendor call
    means no money moved, so the hold must still be released."""
    voids: list[dict[str, object]] = []
    _stub(monkeypatch, _void_tracking_handler(voids))
    metered = meter_fn(FailingAnthropic(KeyboardInterrupt()), agent="a", entity="e")
    with pytest.raises(KeyboardInterrupt):
        metered.messages.create(
            model="claude-opus-5", messages=[{"role": "user", "content": "hi"}]
        )
    assert len(voids) == 1


def test_a_successful_call_never_voids_its_hold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Voiding after money moved would erase a real spend from the ledger."""
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/transactions/authorize":
            return httpx.Response(
                200, json={"authorization_id": "auth_v", "status": "approved"}
            )
        return httpx.Response(
            200,
            json={
                "id": "tx",
                "authorization_id": "auth_v",
                "status": "completed",
                "vendor": "anthropic",
                "amount": 1,
                "currency": "USD",
            },
        )

    _stub(monkeypatch, handler)
    response = SimpleNamespace(
        model="claude-opus-5", usage=SimpleNamespace(input_tokens=1, output_tokens=1)
    )
    metered = meter_fn(FakeAnthropic(response), agent="a", entity="e")
    metered.messages.create(
        model="claude-opus-5", messages=[{"role": "user", "content": "hi"}]
    )
    assert "/transactions/void" not in paths


# --- T5-5: streamed calls must be metered, not silently skipped ---------------


class _FakeStreamManager:
    """Stands in for Anthropic's `messages.stream()` context manager."""

    def __init__(self, final: object, *, entered: list[str]) -> None:
        self._final = final
        self._entered = entered
        self.closed = False

    def __enter__(self) -> _FakeStreamManager:
        self._entered.append("enter")
        return self

    def __exit__(self, *_exc: object) -> bool:
        self.closed = True
        return False

    def get_final_message(self) -> object:
        if self._final is None:
            raise RuntimeError("stream never completed")
        return self._final


class _FakeAnthropicStreamingMessages:
    def __init__(self, final: object, entered: list[str]) -> None:
        self._final = final
        self._entered = entered
        self.stream_calls: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> object:
        raise AssertionError("streaming test must not reach create()")

    def stream(self, **kwargs: object) -> _FakeStreamManager:
        self.stream_calls.append(kwargs)
        return _FakeStreamManager(self._final, entered=self._entered)


class StreamingAnthropic:
    def __init__(self, final: object, entered: list[str] | None = None) -> None:
        self.messages = _FakeAnthropicStreamingMessages(final, entered or [])


StreamingAnthropic.__module__ = "anthropic"


def test_anthropic_messages_stream_is_metered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The T5-5 headline: `messages.stream()` is a separate method, so wrapping
    only `create` left every streamed call unmetered — spend with no ledger row
    and no error."""
    captured: list[dict[str, object]] = []
    _stub(monkeypatch, _capture_handler(captured))
    final = SimpleNamespace(
        model="claude-opus-5",
        usage=SimpleNamespace(input_tokens=1_000_000, output_tokens=0),
    )
    client = StreamingAnthropic(final)
    metered = meter_fn(client, agent="a", entity="e")

    with metered.messages.stream(
        model="claude-opus-5", messages=[{"role": "user", "content": "hi"}]
    ) as stream:
        assert stream.get_final_message() is final

    assert len(client.messages.stream_calls) == 1  # the real stream ran
    assert captured[0]["amount"] == "5.0000"  # and it was booked
    assert captured[0]["tokens"] == 1_000_000


def test_streaming_wrapper_still_delegates_create(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Patching two sibling paths must not clobber either — both `create` and
    `stream` are replaced on the same proxy over `messages`."""
    _stub(monkeypatch, _capture_handler([]))
    client = StreamingAnthropic(None)
    metered = meter_fn(client, agent="a", entity="e")
    assert metered.messages.create is not client.messages.create
    assert metered.messages.stream is not client.messages.stream


def test_a_client_without_the_streaming_helper_is_still_wrapped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An older vendor SDK, or a stub, may not expose `messages.stream` at all —
    that must not break metering of the method that does exist."""
    captured: list[dict[str, object]] = []
    _stub(monkeypatch, _capture_handler(captured))
    response = SimpleNamespace(
        model="claude-opus-5", usage=SimpleNamespace(input_tokens=1, output_tokens=1)
    )
    metered = meter_fn(FakeAnthropic(response), agent="a", entity="e")
    assert metered.messages.create(
        model="claude-opus-5", messages=[{"role": "user", "content": "hi"}]
    ) is response
    assert len(captured) == 1


def test_an_unreadable_stream_books_the_estimate(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Best-effort per T5-1: if the vendor's stream object isn't what we expect,
    the caller's stream still works and the estimate is booked."""
    captured: list[dict[str, object]] = []
    _stub(monkeypatch, _capture_handler(captured))
    metered = meter_fn(StreamingAnthropic(None), agent="a", entity="e")

    with caplog.at_level("WARNING", logger="lucnt.meter"), metered.messages.stream(
        model="claude-opus-5", messages=[{"role": "user", "content": "hi"}]
    ):
        pass

    assert len(captured) == 1
    assert "amount" not in captured[0]  # the hold's estimate stands
    assert "could not read the final message" in caplog.text


def test_a_failed_stream_open_releases_its_hold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    voids: list[dict[str, object]] = []
    _stub(monkeypatch, _void_tracking_handler(voids))

    class _Boom:
        def stream(self, **_kwargs: object) -> object:
            raise RuntimeError("stream refused")

        def create(self, **_kwargs: object) -> object:
            raise AssertionError

    class BoomAnthropic:
        def __init__(self) -> None:
            self.messages = _Boom()

    BoomAnthropic.__module__ = "anthropic"

    metered = meter_fn(BoomAnthropic(), agent="a", entity="e")
    opener = metered.messages.stream
    with pytest.raises(RuntimeError, match="stream refused"):
        opener(model="claude-opus-5", messages=[])
    assert len(voids) == 1


# --- T5-5: stream=True on the primary method ---------------------------------


def _openai_chunks(with_usage: bool) -> list[object]:
    chunks: list[object] = [
        SimpleNamespace(model="gpt-4o", usage=None),
        SimpleNamespace(model="gpt-4o", usage=None),
    ]
    if with_usage:
        chunks.append(
            SimpleNamespace(
                model="gpt-4o",
                usage=SimpleNamespace(prompt_tokens=1000, completion_tokens=500),
            )
        )
    return chunks


def test_openai_stream_true_books_from_the_usage_chunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict[str, object]] = []
    _stub(monkeypatch, _capture_handler(captured))
    metered = meter_fn(FakeOpenAI(iter(_openai_chunks(True))), agent="a", entity="e")

    stream = metered.chat.completions.create(
        model="gpt-4o", messages=[{"role": "user", "content": "hi"}], stream=True
    )
    received = list(stream)

    assert len(received) == 3  # every chunk reached the caller
    assert captured[0]["amount"] == "0.0075"  # settled from the final usage chunk


def test_a_stream_without_usage_books_the_estimate(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """OpenAI omits usage unless stream_options={"include_usage": True}."""
    captured: list[dict[str, object]] = []
    _stub(monkeypatch, _capture_handler(captured))
    metered = meter_fn(FakeOpenAI(iter(_openai_chunks(False))), agent="a", entity="e")

    with caplog.at_level("WARNING", logger="lucnt.meter"):
        list(
            metered.chat.completions.create(
                model="gpt-4o",
                messages=[{"role": "user", "content": "hi"}],
                stream=True,
            )
        )

    assert "amount" not in captured[0]
    assert "include_usage" in caplog.text


def test_nothing_is_booked_until_the_stream_is_consumed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real cost is only known at the end, so booking must wait."""
    captured: list[dict[str, object]] = []
    _stub(monkeypatch, _capture_handler(captured))
    metered = meter_fn(FakeOpenAI(iter(_openai_chunks(True))), agent="a", entity="e")

    stream = metered.chat.completions.create(
        model="gpt-4o", messages=[{"role": "user", "content": "hi"}], stream=True
    )
    assert captured == []  # authorized, not yet captured
    list(stream)
    assert len(captured) == 1


def test_an_abandoned_stream_still_books(monkeypatch: pytest.MonkeyPatch) -> None:
    """Breaking out early is not a reason to lose the spend — the vendor already
    generated and charged for what it streamed."""
    captured: list[dict[str, object]] = []
    _stub(monkeypatch, _capture_handler(captured))
    metered = meter_fn(FakeOpenAI(iter(_openai_chunks(True))), agent="a", entity="e")

    stream = metered.chat.completions.create(
        model="gpt-4o", messages=[{"role": "user", "content": "hi"}], stream=True
    )
    for _ in stream:
        break
    stream.close()  # what garbage collection would do anyway

    assert len(captured) == 1
    assert "amount" not in captured[0]  # no usage chunk was reached


def test_a_stream_failing_midway_books_rather_than_voids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tokens were consumed before the failure, so this is a real spend — unlike a
    call that never started, which is voided."""
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/transactions/authorize":
            return httpx.Response(
                200, json={"authorization_id": "auth_m", "status": "approved"}
            )
        return httpx.Response(
            200,
            json={
                "id": "tx",
                "authorization_id": "auth_m",
                "status": "completed",
                "vendor": "openai",
                "amount": 1,
                "currency": "USD",
            },
        )

    _stub(monkeypatch, handler)

    def exploding_chunks() -> object:
        yield SimpleNamespace(model="gpt-4o", usage=None)
        raise RuntimeError("stream died")

    metered = meter_fn(FakeOpenAI(exploding_chunks()), agent="a", entity="e")
    with pytest.raises(RuntimeError, match="stream died"):
        list(
            metered.chat.completions.create(
                model="gpt-4o",
                messages=[{"role": "user", "content": "hi"}],
                stream=True,
            )
        )

    assert "/transactions/capture" in paths
    assert "/transactions/void" not in paths


def test_a_non_streaming_call_is_unaffected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict[str, object]] = []
    _stub(monkeypatch, _capture_handler(captured))
    response = SimpleNamespace(
        model="gpt-4o", usage=SimpleNamespace(prompt_tokens=1000, completion_tokens=500)
    )
    metered = meter_fn(FakeOpenAI(response), agent="a", entity="e")
    result = metered.chat.completions.create(
        model="gpt-4o", messages=[{"role": "user", "content": "hi"}]
    )
    assert result is response
    assert captured[0]["amount"] == "0.0075"
