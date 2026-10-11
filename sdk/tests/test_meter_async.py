"""`meter()` on async vendor clients: awaited, `stream=True`, async CM, failures."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Iterator
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from lucnt import config, ledger
from lucnt.adapters.anthropic import AnthropicAdapter
from lucnt.adapters.openai import OpenAIAdapter
from lucnt.client import AsyncTransport
from lucnt.exceptions import PolicyBlocked
from lucnt.meter import meter as meter_fn


@pytest.fixture(autouse=True)
def _configured() -> Iterator[None]:
    config.configure(api_key="sk_test", base_url="https://api.test")
    yield
    config._reset_config()


@pytest.fixture(autouse=True)
def _reset_async_transport_cache() -> Iterator[None]:
    ledger._async_transports.clear()
    yield
    ledger._async_transports.clear()


def _async_stub(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    pools: dict[asyncio.AbstractEventLoop, AsyncTransport] = {}

    def _get() -> AsyncTransport:
        loop = asyncio.get_running_loop()
        if loop not in pools:
            pools[loop] = AsyncTransport(
                config.get_config(), transport=httpx.MockTransport(handler)
            )
        return pools[loop]

    monkeypatch.setattr(ledger, "_get_async_transport", _get)


def _ledger_recorder(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Record the path and body of every backend call an async meter makes."""
    paths: list[str] = []
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        bodies.append(json.loads(request.content) if request.content else {})
        if request.url.path.endswith("authorize"):
            return httpx.Response(
                200,
                json={
                    "authorization_id": "auth_1",
                    "status": "approved",
                    "reason": None,
                },
            )
        if request.url.path.endswith("void"):
            return httpx.Response(
                200,
                json={
                    "authorization_id": "auth_1",
                    "released_at": "2026-07-28T10:00:00Z",
                    "release_reason": None,
                    "already_released": False,
                },
            )
        return httpx.Response(
            200,
            json={
                "id": "tx_1",
                "authorization_id": "auth_1",
                "status": "booked",
                "vendor": "openai",
                "amount": "1.00",
                "currency": "USD",
            },
        )

    _async_stub(monkeypatch, handler)
    return paths, bodies


# --- Fake async vendor clients (module-spoofed) ----------------------------- #


def _usage(input_tokens: int = 1_000, output_tokens: int = 500) -> SimpleNamespace:
    return SimpleNamespace(
        prompt_tokens=input_tokens,
        completion_tokens=output_tokens,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


def _vendor_net() -> httpx.Client:
    """See test_meter.py's twin: fakes model the `._client` a real vendor
    client always carries, so meter() can install the governor."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected wire request: {request.method} {request.url}")

    return httpx.Client(transport=httpx.MockTransport(handler))


def _vendor_net_async() -> httpx.AsyncClient:
    """Async twin of `_vendor_net` for the `Async...` fakes."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected wire request: {request.method} {request.url}")

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class _AsyncOpenAICompletions:
    def __init__(self, response: object) -> None:
        self._response = response
        self.calls: list[dict[str, object]] = []

    async def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        return self._response


class AsyncOpenAI:
    def __init__(self, response: object) -> None:
        self.chat = SimpleNamespace(completions=_AsyncOpenAICompletions(response))
        self.other_attr = "delegated"
        self._client = _vendor_net_async()


AsyncOpenAI.__module__ = "openai"


class _FailingAsyncOpenAICompletions:
    async def create(self, **kwargs: object) -> object:
        raise RuntimeError("vendor exploded")


class FailingAsyncOpenAI:
    def __init__(self) -> None:
        self.chat = SimpleNamespace(completions=_FailingAsyncOpenAICompletions())
        self._client = _vendor_net_async()


FailingAsyncOpenAI.__module__ = "openai"


class _AsyncStreamingOpenAICompletions:
    def __init__(self, chunks: list[object]) -> None:
        self._chunks = chunks

    async def create(self, **kwargs: object) -> AsyncIterator[object]:
        async def _iter() -> AsyncIterator[object]:
            for chunk in self._chunks:
                yield chunk

        return _iter()


class StreamingAsyncOpenAI:
    def __init__(self, chunks: list[object]) -> None:
        self.chat = SimpleNamespace(
            completions=_AsyncStreamingOpenAICompletions(chunks)
        )
        self._client = _vendor_net_async()


StreamingAsyncOpenAI.__module__ = "openai"


class _FakeAsyncStream:
    def __init__(self, final: object) -> None:
        self._final = final
        self.events: list[str] = []

    async def get_final_message(self) -> object:
        return self._final

    async def __anext__(self) -> object:
        raise StopAsyncIteration

    def __aiter__(self) -> _FakeAsyncStream:
        return self


class _FakeAsyncStreamManager:
    def __init__(self, final: object) -> None:
        self.stream = _FakeAsyncStream(final)
        self.entered = False
        self.exited = False

    async def __aenter__(self) -> _FakeAsyncStream:
        self.entered = True
        return self.stream

    async def __aexit__(self, *exc_info: object) -> bool:
        self.exited = True
        return False


class _AsyncAnthropicMessages:
    def __init__(self, response: object, final: object) -> None:
        self._response = response
        self.manager = _FakeAsyncStreamManager(final)
        self.create_calls: list[dict[str, object]] = []
        self.stream_calls: list[dict[str, object]] = []

    async def create(self, **kwargs: object) -> object:
        self.create_calls.append(kwargs)
        return self._response

    def stream(self, **kwargs: object) -> _FakeAsyncStreamManager:
        """Deliberately sync, like the real SDK: returns an async CM."""
        self.stream_calls.append(kwargs)
        return self.manager


class AsyncAnthropic:
    def __init__(self, response: object, final: object) -> None:
        self.messages = _AsyncAnthropicMessages(response, final)
        self._client = _vendor_net_async()


AsyncAnthropic.__module__ = "anthropic"


def _openai_response(model: str = "gpt-4o") -> SimpleNamespace:
    return SimpleNamespace(model=model, usage=_usage())


def _anthropic_response(model: str = "claude-opus-5") -> SimpleNamespace:
    return SimpleNamespace(model=model, usage=_usage())


# --- Detection -------------------------------------------------------------- #


def test_an_async_openai_client_is_detected_as_async() -> None:
    adapter = OpenAIAdapter()
    assert adapter.is_async(AsyncOpenAI(_openai_response())) is True


def test_a_sync_openai_client_is_not_detected_as_async() -> None:
    class _SyncCompletions:
        def create(self, **kwargs: object) -> object:
            return _openai_response()

    class SyncOpenAI:
        def __init__(self) -> None:
            self.chat = SimpleNamespace(completions=_SyncCompletions())

    SyncOpenAI.__module__ = "openai"
    assert OpenAIAdapter().is_async(SyncOpenAI()) is False


def test_detection_prefers_the_method_over_the_class_name() -> None:
    """A coroutine method marks a client async even without an `Async...` name."""

    class _Completions:
        async def create(self, **kwargs: object) -> object:
            return _openai_response()

    class Client:  # no "Async" anywhere in the name
        def __init__(self) -> None:
            self.chat = SimpleNamespace(completions=_Completions())

    Client.__module__ = "openai"
    assert OpenAIAdapter().is_async(Client()) is True


def test_meter_no_longer_rejects_an_async_client() -> None:
    """`meter()` accepts an async client."""
    metered = meter_fn(AsyncOpenAI(_openai_response()), agent="a", entity="e")
    assert asyncio.iscoroutinefunction(metered.chat.completions.create)


# --- The plain awaited completion ------------------------------------------ #


def test_an_async_completion_is_authorized_then_captured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, bodies = _ledger_recorder(monkeypatch)
    client = AsyncOpenAI(_openai_response())
    metered = meter_fn(client, agent="research", entity="in")

    async def main() -> object:
        return await metered.chat.completions.create(
            model="gpt-4o", messages=[{"role": "user", "content": "hi"}]
        )

    response = asyncio.run(main())
    assert response.model == "gpt-4o"  # type: ignore[attr-defined]
    assert paths == ["/transactions/authorize", "/transactions/capture"]
    # Settled with the real usage, not left at the estimate.
    assert bodies[1]["tokens"] == 1_500
    assert bodies[1]["model"] == "gpt-4o"


def test_an_async_completion_blocked_by_policy_never_reaches_the_vendor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Raising *before* the vendor call is the gate working — no money moved."""
    _async_stub(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            json={
                "authorization_id": "auth_2",
                "status": "blocked",
                "code": "budget_exhausted",
                "reason": "over monthly budget",
            },
        ),
    )
    client = AsyncOpenAI(_openai_response())
    metered = meter_fn(client, agent="research", entity="in")

    async def main() -> object:
        return await metered.chat.completions.create(
            model="gpt-4o", messages=[{"role": "user", "content": "hi"}]
        )

    with pytest.raises(PolicyBlocked):
        asyncio.run(main())
    assert client.chat.completions.calls == []


def test_a_failing_async_vendor_call_releases_the_hold_and_reraises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Void on the async path: a hold nobody captures reserves budget forever."""
    paths, _ = _ledger_recorder(monkeypatch)
    metered = meter_fn(FailingAsyncOpenAI(), agent="research", entity="in")

    async def main() -> object:
        return await metered.chat.completions.create(
            model="gpt-4o", messages=[{"role": "user", "content": "hi"}]
        )

    with pytest.raises(RuntimeError, match="vendor exploded"):
        asyncio.run(main())
    assert paths == ["/transactions/authorize", "/transactions/void"]


def test_a_backend_failure_after_an_async_call_never_destroys_the_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Best-effort booking on the async path — the response is the thing the
    customer bought."""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if request.url.path.endswith("authorize"):
            return httpx.Response(
                200,
                json={
                    "authorization_id": "auth_1",
                    "status": "approved",
                    "reason": None,
                },
            )
        return httpx.Response(500)  # capture is down

    _async_stub(monkeypatch, handler)
    metered = meter_fn(AsyncOpenAI(_openai_response()), agent="a", entity="e")

    async def main() -> object:
        return await metered.chat.completions.create(
            model="gpt-4o", messages=[{"role": "user", "content": "hi"}]
        )

    response = asyncio.run(main())
    assert response.model == "gpt-4o"  # type: ignore[attr-defined]


def test_unwrapped_attributes_still_delegate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metered = meter_fn(AsyncOpenAI(_openai_response()), agent="a", entity="e")
    assert metered.other_attr == "delegated"


# --- stream=True on the awaited completion --------------------------------- #


def test_an_async_stream_is_passed_through_and_booked_at_the_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, bodies = _ledger_recorder(monkeypatch)
    chunks = [
        SimpleNamespace(model="gpt-4o", usage=None),
        SimpleNamespace(model="gpt-4o", usage=_usage(2_000, 100)),
    ]
    metered = meter_fn(StreamingAsyncOpenAI(chunks), agent="a", entity="e")

    async def main() -> list[object]:
        stream = await metered.chat.completions.create(
            model="gpt-4o", messages=[{"role": "user", "content": "hi"}], stream=True
        )
        return [chunk async for chunk in stream]

    received = asyncio.run(main())
    assert len(received) == 2
    assert paths == ["/transactions/authorize", "/transactions/capture"]
    assert bodies[1]["tokens"] == 2_100


def test_an_abandoned_async_stream_is_still_booked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Breaking out of a stream early books what was streamed instead of voiding."""
    paths, _ = _ledger_recorder(monkeypatch)
    chunks = [
        SimpleNamespace(model="gpt-4o", usage=_usage(10, 10)),
        SimpleNamespace(model="gpt-4o", usage=_usage(20, 20)),
        SimpleNamespace(model="gpt-4o", usage=_usage(30, 30)),
    ]
    metered = meter_fn(StreamingAsyncOpenAI(chunks), agent="a", entity="e")

    async def main() -> None:
        stream = await metered.chat.completions.create(
            model="gpt-4o", messages=[{"role": "user", "content": "hi"}], stream=True
        )
        async for _chunk in stream:
            break  # caller stops early

    asyncio.run(main())
    assert "/transactions/capture" in paths
    assert "/transactions/void" not in paths


def test_an_async_stream_with_no_usage_chunk_books_the_estimate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, bodies = _ledger_recorder(monkeypatch)
    chunks = [SimpleNamespace(model="gpt-4o", usage=None)]
    metered = meter_fn(StreamingAsyncOpenAI(chunks), agent="a", entity="e")

    async def main() -> list[object]:
        stream = await metered.chat.completions.create(
            model="gpt-4o", messages=[{"role": "user", "content": "hi"}], stream=True
        )
        return [chunk async for chunk in stream]

    asyncio.run(main())
    assert paths == ["/transactions/authorize", "/transactions/capture"]
    assert "amount" not in bodies[1]  # the hold's estimate stands


def test_an_async_stream_that_errors_midway_is_booked_then_reraises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, _ = _ledger_recorder(monkeypatch)

    class _Exploding:
        async def create(self, **kwargs: object) -> AsyncIterator[object]:
            async def _iter() -> AsyncIterator[object]:
                yield SimpleNamespace(model="gpt-4o", usage=_usage(5, 5))
                raise RuntimeError("stream died")

            return _iter()

    class Client:
        def __init__(self) -> None:
            self.chat = SimpleNamespace(completions=_Exploding())
            self._client = _vendor_net_async()

    Client.__module__ = "openai"
    metered = meter_fn(Client(), agent="a", entity="e")

    async def main() -> None:
        stream = await metered.chat.completions.create(
            model="gpt-4o", messages=[{"role": "user", "content": "hi"}], stream=True
        )
        async for _chunk in stream:
            pass

    with pytest.raises(RuntimeError, match="stream died"):
        asyncio.run(main())
    assert "/transactions/capture" in paths


# --- AsyncAnthropic.messages.stream() — the async context manager ---------- #


def test_the_async_stream_helper_is_metered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`AsyncAnthropic.messages.stream()` is metered, not silent spend."""
    paths, bodies = _ledger_recorder(monkeypatch)
    client = AsyncAnthropic(_anthropic_response(), _anthropic_response())
    metered = meter_fn(client, agent="a", entity="e")

    async def main() -> None:
        async with metered.messages.stream(
            model="claude-opus-5", messages=[{"role": "user", "content": "hi"}]
        ) as stream:
            assert stream is client.messages.manager.stream

    asyncio.run(main())
    assert paths == ["/transactions/authorize", "/transactions/capture"]
    assert bodies[1]["tokens"] == 1_500
    assert client.messages.manager.entered
    assert client.messages.manager.exited


def test_the_async_stream_hold_is_estimated_from_the_whole_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The streamed hold scales with the prompt, not priced as an empty request."""
    _, bodies = _ledger_recorder(monkeypatch)
    client = AsyncAnthropic(_anthropic_response(), _anthropic_response())
    metered = meter_fn(client, agent="a", entity="e")

    async def main(prompt: str) -> None:
        async with metered.messages.stream(
            model="claude-opus-5",
            max_tokens=16,
            messages=[{"role": "user", "content": prompt}],
        ):
            pass

    asyncio.run(main("x" * 400_000))
    big_hold = bodies[0]["amount"]
    bodies.clear()
    asyncio.run(main("hi"))
    small_hold = bodies[0]["amount"]
    from decimal import Decimal

    assert Decimal(str(big_hold)) > Decimal(str(small_hold))


def test_the_async_stream_helper_returns_a_context_manager_not_a_coroutine() -> None:
    """`async with client.messages.stream(...)` works: the wrapper must stay sync."""
    client = AsyncAnthropic(_anthropic_response(), _anthropic_response())
    metered = meter_fn(client, agent="a", entity="e")
    manager = metered.messages.stream(model="claude-opus-5", messages=[])
    assert not asyncio.iscoroutine(manager)
    assert hasattr(manager, "__aenter__")


def test_no_hold_is_taken_until_the_async_stream_is_entered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A manager built and never entered must not reserve budget."""
    paths, _ = _ledger_recorder(monkeypatch)
    client = AsyncAnthropic(_anthropic_response(), _anthropic_response())
    metered = meter_fn(client, agent="a", entity="e")
    metered.messages.stream(model="claude-opus-5", messages=[])
    assert paths == []


def test_a_failing_async_stream_entry_releases_the_hold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, _ = _ledger_recorder(monkeypatch)

    class _Failing:
        async def __aenter__(self) -> object:
            raise RuntimeError("cannot open stream")

        async def __aexit__(self, *exc_info: object) -> bool:
            return False

    class _Messages:
        def stream(self, **kwargs: object) -> _Failing:
            return _Failing()

        async def create(self, **kwargs: object) -> object:
            return _anthropic_response()

    class Client:
        def __init__(self) -> None:
            self.messages = _Messages()
            self._client = _vendor_net_async()

    Client.__module__ = "anthropic"
    metered = meter_fn(Client(), agent="a", entity="e")

    async def main() -> None:
        async with metered.messages.stream(model="claude-opus-5", messages=[]):
            pass

    with pytest.raises(RuntimeError, match="cannot open stream"):
        asyncio.run(main())
    assert paths == ["/transactions/authorize", "/transactions/void"]


def test_an_unreadable_async_final_message_books_the_estimate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Best-effort: the caller's stream must survive our ignorance."""
    paths, bodies = _ledger_recorder(monkeypatch)

    class _Stream:
        async def get_final_message(self) -> object:
            raise AttributeError("no such helper")

    class _Manager:
        async def __aenter__(self) -> _Stream:
            return _Stream()

        async def __aexit__(self, *exc_info: object) -> bool:
            return False

    class _Messages:
        def stream(self, **kwargs: object) -> _Manager:
            return _Manager()

        async def create(self, **kwargs: object) -> object:
            return _anthropic_response()

    class Client:
        def __init__(self) -> None:
            self.messages = _Messages()
            self._client = _vendor_net_async()

    Client.__module__ = "anthropic"
    metered = meter_fn(Client(), agent="a", entity="e")

    async def main() -> None:
        async with metered.messages.stream(model="claude-opus-5", messages=[]):
            pass

    asyncio.run(main())
    assert paths == ["/transactions/authorize", "/transactions/capture"]
    assert "amount" not in bodies[1]


# --- Concurrency ----------------------------------------------------------- #


def test_concurrent_metered_async_calls_each_book_separately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reason async matters: many calls in flight at once, each attributed."""
    paths, _ = _ledger_recorder(monkeypatch)
    metered = meter_fn(AsyncOpenAI(_openai_response()), agent="a", entity="e")

    async def main() -> None:
        await asyncio.gather(
            *(
                metered.chat.completions.create(
                    model="gpt-4o", messages=[{"role": "user", "content": str(n)}]
                )
                for n in range(8)
            )
        )

    asyncio.run(main())
    assert paths.count("/transactions/authorize") == 8
    assert paths.count("/transactions/capture") == 8


def test_the_sync_adapter_still_meters_a_sync_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Widening detection must not reroute sync clients through the async path."""
    from lucnt.client import Transport

    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("authorize"):
            return httpx.Response(
                200,
                json={
                    "authorization_id": "auth_1",
                    "status": "approved",
                    "reason": None,
                },
            )
        return httpx.Response(
            200,
            json={
                "id": "tx_1",
                "authorization_id": "auth_1",
                "status": "booked",
                "vendor": "anthropic",
                "amount": "1.00",
                "currency": "USD",
            },
        )

    transport = Transport(config.get_config(), transport=httpx.MockTransport(handler))
    monkeypatch.setattr(ledger, "_get_transport", lambda: transport)

    class _Messages:
        def create(self, **kwargs: object) -> object:
            return _anthropic_response()

    class SyncAnthropic:
        def __init__(self) -> None:
            self.messages = _Messages()
            self._client = _vendor_net()

    SyncAnthropic.__module__ = "anthropic"
    metered = meter_fn(SyncAnthropic(), agent="a", entity="e")
    result = metered.messages.create(
        model="claude-opus-5", messages=[{"role": "user", "content": "hi"}]
    )
    assert result.model == "claude-opus-5"
    assert paths == ["/transactions/authorize", "/transactions/capture"]


def test_the_adapter_serves_both_flavours_of_one_vendor() -> None:
    """One adapter, two client shapes — vendor knowledge does not fork."""
    assert AnthropicAdapter.matches(
        AsyncAnthropic(_anthropic_response(), _anthropic_response())
    )
    assert AnthropicAdapter().vendor == "anthropic"
