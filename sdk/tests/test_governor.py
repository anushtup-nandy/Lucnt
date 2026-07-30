"""Unit tests for the transport-layer governor (F-1).

The governor is exercised directly through `httpx.Client` objects whose inner
transport is an `httpx.MockTransport` playing the vendor — no vendor SDK is
needed to test the wire semantics. The lucnt backend is stubbed exactly as in
test_meter.py. What only a *real* vendor client can prove (that its every call
path reaches the one transport) lives in tests-realclient/, not here.
"""

from __future__ import annotations

import asyncio
import gzip
import json
from collections.abc import Callable, Iterator
from decimal import Decimal

import httpx
import pytest

from lucnt import config, governor, ledger
from lucnt.adapters.base import Usage
from lucnt.adapters.openai import OpenAIAdapter
from lucnt.client import AsyncTransport, Transport
from lucnt.exceptions import LucntError
from lucnt.governor import (
    GovernedAsyncTransport,
    GovernedTransport,
    _classify,
    _estimate,
    _SseUsageScanner,
    _usage_from_payload,
    metered_async_http_client,
    metered_http_client,
    wrapper_governed,
)


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


class _Ledger:
    """Records ledger traffic and answers with a configurable verdict."""

    def __init__(self, status: str = "approved", code: str = "ok") -> None:
        self.status = status
        self.code = code
        self.authorizes: list[dict[str, object]] = []
        self.captures: list[dict[str, object]] = []
        self.voids: list[dict[str, object]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        path = request.url.path
        if path == "/transactions/authorize":
            self.authorizes.append(body)
            return httpx.Response(
                200,
                json={
                    "authorization_id": f"auth_{len(self.authorizes)}",
                    "status": self.status,
                    "code": self.code,
                    "reason": "policy says no" if self.status != "approved" else None,
                },
            )
        if path == "/transactions/capture":
            self.captures.append(body)
            return httpx.Response(
                200,
                json={
                    "id": f"tx_{len(self.captures)}",
                    "authorization_id": body["authorization_id"],
                    "status": "completed",
                    "vendor": "openai",
                    "amount": str(body.get("amount", "1")),
                    "currency": "USD",
                    "estimated": bool(body.get("estimated", False)),
                },
            )
        if path == "/transactions/void":
            self.voids.append(body)
            return httpx.Response(
                200,
                json={
                    "authorization_id": body["authorization_id"],
                    "released_at": "2026-07-28T10:00:00Z",
                    "already_released": False,
                },
            )
        raise AssertionError(f"unexpected ledger path {path}")


def _stub_ledger(monkeypatch: pytest.MonkeyPatch, recorder: _Ledger) -> None:
    transport = Transport(
        config.get_config(), transport=httpx.MockTransport(recorder.handler)
    )
    monkeypatch.setattr(ledger, "_get_transport", lambda: transport)


def _stub_async_ledger(monkeypatch: pytest.MonkeyPatch, recorder: _Ledger) -> None:
    pools: dict[asyncio.AbstractEventLoop, AsyncTransport] = {}

    def _get() -> AsyncTransport:
        loop = asyncio.get_running_loop()
        if loop not in pools:
            pools[loop] = AsyncTransport(
                config.get_config(),
                transport=httpx.MockTransport(recorder.handler),
            )
        return pools[loop]

    monkeypatch.setattr(ledger, "_get_async_transport", _get)


# --- vendor-side fake responses ----------------------------------------------


_CHAT_USAGE = {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}


def _chat_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-1",
            "model": "gpt-4o",
            "choices": [{"message": {"role": "assistant", "content": "hi"}}],
            "usage": _CHAT_USAGE,
        },
    )


def _governed(
    vendor_handler: Callable[[httpx.Request], httpx.Response],
    *,
    unmetered: str = "block",
) -> tuple[httpx.Client, GovernedTransport]:
    transport = GovernedTransport(
        httpx.MockTransport(vendor_handler),
        OpenAIAdapter(),
        agent="a",
        entity="e",
        intent=None,
        project=None,
        unmetered=unmetered,  # type: ignore[arg-type]
    )
    client = httpx.Client(transport=transport, base_url="https://api.openai.com")
    return client, transport


_CHAT_BODY = {
    "model": "gpt-4o",
    "messages": [{"role": "user", "content": "hello there"}],
}


# --- classification -----------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/v1/chat/completions", ("openai", "chat")),
        ("/v1/responses", ("openai", "responses")),
        ("/v1/embeddings", ("openai", "embeddings")),
        ("/v1/completions", ("openai", "legacy")),
        ("/v1/messages", ("anthropic", "messages")),
        ("/v1/messages/count_tokens", None),
        ("/v1/files", "unknown"),
        ("/v1/audio/speech", "unknown"),
    ],
)
def test_post_paths_classify(path: str, expected: object) -> None:
    assert _classify("POST", path) == expected


def test_reads_pass_through_unclassified() -> None:
    assert _classify("GET", "/v1/models") is None
    assert _classify("DELETE", "/v1/files/abc") is None


# --- endpoint estimators -------------------------------------------------------


def test_embeddings_estimate_covers_every_input_shape() -> None:
    # 100k tokens keeps the arithmetic above the ledger's 4dp quantum, so the
    # shape comparisons below are exact rather than floored.
    adapter = OpenAIAdapter()
    text = "x" * 400_000  # 100k tokens at 4 chars/token
    per_100k_tokens = _estimate(
        adapter, "embeddings", {"model": "text-embedding-3-small", "input": text}
    )
    assert per_100k_tokens == Decimal("0.0020")  # 100k x $0.02/1M
    doubled = _estimate(
        adapter,
        "embeddings",
        {"model": "text-embedding-3-small", "input": [text, text]},
    )
    assert doubled == per_100k_tokens * 2
    # Pre-tokenized inputs: a list of token ids, and a batch of them.
    assert (
        _estimate(
            adapter,
            "embeddings",
            {"model": "text-embedding-3-small", "input": [1] * 100_000},
        )
        == per_100k_tokens
    )
    assert (
        _estimate(
            adapter,
            "embeddings",
            {
                "model": "text-embedding-3-small",
                "input": [[1] * 100_000, [2] * 100_000],
            },
        )
        == per_100k_tokens * 2
    )


def test_a_subcent_spend_books_the_minimum_ledger_unit() -> None:
    """8 embedding tokens cost ~$0.0000002 — quantized to 0.0000 the backend
    would refuse it (`amount > 0`). A positive spend floors at 0.0001 instead
    of vanishing."""
    tiny = _estimate(
        adapter := OpenAIAdapter(),
        "embeddings",
        {"model": "text-embedding-3-small", "input": "hello"},
    )
    assert tiny == Decimal("0.0001")
    assert adapter.cost(
        Usage(model="text-embedding-3-small", input_tokens=8, output_tokens=0)
    ) == Decimal("0.0001")


def test_responses_estimate_reads_input_instructions_and_cap() -> None:
    adapter = OpenAIAdapter()
    small = _estimate(
        adapter,
        "responses",
        {"model": "gpt-4o", "input": "hi", "max_output_tokens": 16},
    )
    bigger_prompt = _estimate(
        adapter,
        "responses",
        {
            "model": "gpt-4o",
            "input": [
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "y" * 4000}],
                }
            ],
            "instructions": "z" * 4000,
            "max_output_tokens": 16,
        },
    )
    bigger_cap = _estimate(
        adapter,
        "responses",
        {"model": "gpt-4o", "input": "hi", "max_output_tokens": 4096},
    )
    assert bigger_prompt > small
    assert bigger_cap > small


def test_legacy_completions_estimate_reads_the_prompt() -> None:
    adapter = OpenAIAdapter()
    small = _estimate(
        adapter, "legacy", {"model": "gpt-3.5-turbo", "prompt": "hi", "max_tokens": 16}
    )
    big = _estimate(
        adapter,
        "legacy",
        {
            "model": "gpt-3.5-turbo",
            "prompt": ["x" * 4000, "y" * 4000],
            "max_tokens": 16,
        },
    )
    assert big > small


def test_chat_estimate_delegates_to_the_adapter() -> None:
    adapter = OpenAIAdapter()
    assert _estimate(adapter, "chat", _CHAT_BODY) == adapter.estimate_cost(_CHAT_BODY)


# --- usage readers -------------------------------------------------------------


def test_usage_readers_per_endpoint_kind() -> None:
    chat = _usage_from_payload("chat", {"model": "gpt-4o", "usage": _CHAT_USAGE})
    assert chat == Usage(model="gpt-4o", input_tokens=100, output_tokens=50)
    emb = _usage_from_payload(
        "embeddings",
        {
            "model": "text-embedding-3-small",
            "usage": {"prompt_tokens": 8, "total_tokens": 8},
        },
    )
    assert emb == Usage(model="text-embedding-3-small", input_tokens=8, output_tokens=0)
    resp = _usage_from_payload(
        "responses",
        {"model": "gpt-4o", "usage": {"input_tokens": 36, "output_tokens": 87}},
    )
    assert resp == Usage(model="gpt-4o", input_tokens=36, output_tokens=87)
    msg = _usage_from_payload(
        "messages",
        {"model": "claude-opus-5", "usage": {"input_tokens": 10, "output_tokens": 25}},
    )
    assert msg == Usage(model="claude-opus-5", input_tokens=10, output_tokens=25)


def test_usage_reader_raises_on_a_usage_free_payload() -> None:
    with pytest.raises(LucntError):
        _usage_from_payload("chat", {"model": "gpt-4o"})


# --- SSE usage scanner ----------------------------------------------------------


def _feed_lines(scanner: _SseUsageScanner, events: list[dict[str, object]]) -> None:
    for event in events:
        scanner.feed(f"data: {json.dumps(event)}\n\n".encode())


def test_scanner_reads_the_anthropic_event_shape() -> None:
    scanner = _SseUsageScanner()
    _feed_lines(
        scanner,
        [
            {
                "type": "message_start",
                "message": {"model": "claude-opus-5", "usage": {"input_tokens": 17}},
            },
            {"type": "content_block_delta", "delta": {"text": "hi"}},
            {"type": "message_delta", "usage": {"output_tokens": 42}},
        ],
    )
    assert scanner.saw_usage
    assert (scanner.model, scanner.input_tokens, scanner.output_tokens) == (
        "claude-opus-5",
        17,
        42,
    )


def test_scanner_reads_the_openai_chat_shape() -> None:
    scanner = _SseUsageScanner()
    _feed_lines(
        scanner,
        [
            {
                "model": "gpt-4o",
                "choices": [{"delta": {"content": "h"}}],
                "usage": None,
            },
            {"model": "gpt-4o", "choices": [], "usage": _CHAT_USAGE},
        ],
    )
    scanner.feed(b"data: [DONE]\n\n")
    assert scanner.saw_usage
    assert (scanner.input_tokens, scanner.output_tokens) == (100, 50)


def test_scanner_reads_the_openai_responses_shape() -> None:
    scanner = _SseUsageScanner()
    _feed_lines(
        scanner,
        [
            {"type": "response.output_text.delta", "delta": "h"},
            {
                "type": "response.completed",
                "response": {
                    "model": "gpt-4o",
                    "usage": {"input_tokens": 36, "output_tokens": 87},
                },
            },
        ],
    )
    assert scanner.saw_usage
    assert (scanner.model, scanner.input_tokens, scanner.output_tokens) == (
        "gpt-4o",
        36,
        87,
    )


def test_scanner_survives_events_split_across_chunks() -> None:
    scanner = _SseUsageScanner()
    event = json.dumps({"model": "gpt-4o", "usage": _CHAT_USAGE})
    payload = f"data: {event}\n\n".encode()
    scanner.feed(payload[:19])
    scanner.feed(payload[19:])
    assert scanner.saw_usage


def test_scanner_reports_a_usage_free_stream() -> None:
    scanner = _SseUsageScanner()
    _feed_lines(scanner, [{"model": "gpt-4o", "choices": []}])
    assert not scanner.saw_usage


# --- governed transport: sync ----------------------------------------------------


def test_an_approved_call_is_authorized_then_settled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _Ledger()
    _stub_ledger(monkeypatch, recorder)
    client, _ = _governed(_chat_handler)

    response = client.post("/v1/chat/completions", json=_CHAT_BODY)

    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "hi"
    assert len(recorder.authorizes) == 1
    assert recorder.authorizes[0]["agent"] == "a"
    assert recorder.authorizes[0]["model"] == "gpt-4o"
    expected = OpenAIAdapter().cost(
        Usage(model="gpt-4o", input_tokens=100, output_tokens=50)
    )
    assert len(recorder.captures) == 1
    assert Decimal(str(recorder.captures[0]["amount"])) == expected
    assert recorder.captures[0]["tokens"] == 150
    assert "estimated" not in recorder.captures[0]
    assert recorder.voids == []


def test_a_blocked_call_never_reaches_the_vendor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _Ledger(status="blocked", code="budget_exhausted")
    _stub_ledger(monkeypatch, recorder)
    vendor_hits: list[str] = []

    def vendor(request: httpx.Request) -> httpx.Response:
        vendor_hits.append(request.url.path)
        return _chat_handler(request)

    client, _ = _governed(vendor)
    response = client.post("/v1/chat/completions", json=_CHAT_BODY)

    assert response.status_code == 403
    body = response.json()["error"]
    assert body["type"] == "lucnt_policy_blocked"
    assert body["code"] == "budget_exhausted"
    assert "No money moved" in body["message"]
    assert response.headers["x-lucnt-blocked"] == "budget_exhausted"
    assert vendor_hits == []
    assert recorder.captures == []


def test_an_unknown_spend_path_is_blocked_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _Ledger()
    _stub_ledger(monkeypatch, recorder)
    vendor_hits: list[str] = []

    def vendor(request: httpx.Request) -> httpx.Response:
        vendor_hits.append(request.url.path)
        return httpx.Response(200, json={})

    client, _ = _governed(vendor)
    response = client.post("/v1/audio/speech", json={"model": "tts-1", "input": "hi"})

    assert response.status_code == 403
    assert response.json()["error"]["type"] == "lucnt_unmetered_path"
    assert "/v1/audio/speech" in response.json()["error"]["message"]
    assert vendor_hits == []
    assert recorder.authorizes == []


def test_unmetered_allow_forwards_with_a_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    recorder = _Ledger()
    _stub_ledger(monkeypatch, recorder)
    client, _ = _governed(
        lambda request: httpx.Response(200, json={"ok": True}), unmetered="allow"
    )

    with caplog.at_level("WARNING", logger="lucnt.governor"):
        first = client.post("/v1/audio/speech", json={"input": "hi"})
        second = client.post("/v1/audio/speech", json={"input": "ho"})

    assert first.status_code == 200 and second.status_code == 200
    assert recorder.authorizes == []
    # warned once per path, not per request
    assert caplog.text.count("/v1/audio/speech") == 1


def test_reads_and_free_posts_pass_through_ungoverned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _Ledger()
    _stub_ledger(monkeypatch, recorder)
    client, _ = _governed(lambda request: httpx.Response(200, json={"data": []}))

    assert client.get("/v1/models").status_code == 200
    assert (
        client.post("/v1/messages/count_tokens", json={"model": "x"}).status_code == 200
    )
    assert recorder.authorizes == []


def test_a_vendor_error_releases_the_hold_and_passes_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _Ledger()
    _stub_ledger(monkeypatch, recorder)
    client, _ = _governed(
        lambda request: httpx.Response(500, json={"error": "vendor down"})
    )

    response = client.post("/v1/chat/completions", json=_CHAT_BODY)

    assert response.status_code == 500  # untouched
    assert len(recorder.authorizes) == 1
    assert len(recorder.voids) == 1
    assert recorder.captures == []


def test_a_wire_exception_releases_the_hold_and_reraises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _Ledger()
    _stub_ledger(monkeypatch, recorder)

    def vendor(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    client, _ = _governed(vendor)
    with pytest.raises(httpx.ConnectError):
        client.post("/v1/chat/completions", json=_CHAT_BODY)

    assert len(recorder.voids) == 1
    assert recorder.captures == []


def test_an_unpriced_model_fails_closed_and_loud(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _Ledger()
    _stub_ledger(monkeypatch, recorder)
    vendor_hits: list[str] = []

    def vendor(request: httpx.Request) -> httpx.Response:
        vendor_hits.append(request.url.path)
        return _chat_handler(request)

    client, _ = _governed(vendor)
    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-nonexistent",
            "messages": [{"role": "user", "content": "x"}],
        },
    )

    assert response.status_code == 403
    assert response.json()["error"]["type"] == "lucnt_unpriceable"
    assert "gpt-nonexistent" in response.json()["error"]["message"]
    assert vendor_hits == []


def test_an_unparseable_body_on_a_spend_path_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _Ledger()
    _stub_ledger(monkeypatch, recorder)
    client, _ = _governed(_chat_handler)
    response = client.post(
        "/v1/chat/completions",
        content=b"\x00not json",
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 403
    assert response.json()["error"]["type"] == "lucnt_unpriceable"


def test_the_wrapper_token_skips_the_net(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """meter()'s method wrappers authorize themselves; the net must not
    double-book the same logical call."""
    recorder = _Ledger()
    _stub_ledger(monkeypatch, recorder)
    client, _ = _governed(_chat_handler)

    with wrapper_governed():
        response = client.post("/v1/chat/completions", json=_CHAT_BODY)

    assert response.status_code == 200
    assert recorder.authorizes == []
    assert recorder.captures == []


def test_a_streamed_response_passes_chunks_and_books_actual_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _Ledger()
    _stub_ledger(monkeypatch, recorder)
    events = (
        b'data: {"model":"gpt-4o","choices":[{"delta":{"content":"h"}}],'
        b'"usage":null}\n\n'
        b'data: {"model":"gpt-4o","choices":[],'
        b'"usage":{"prompt_tokens":10,"completion_tokens":5}}\n\n'
        b"data: [DONE]\n\n"
    )

    def vendor(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=events
        )

    client, _ = _governed(vendor)
    with client.stream(
        "POST", "/v1/chat/completions", json={**_CHAT_BODY, "stream": True}
    ) as response:
        received = b"".join(response.iter_bytes())

    assert received == events  # byte-identical passthrough
    assert len(recorder.captures) == 1
    expected = OpenAIAdapter().cost(
        Usage(model="gpt-4o", input_tokens=10, output_tokens=5)
    )
    assert Decimal(str(recorder.captures[0]["amount"])) == expected
    assert "estimated" not in recorder.captures[0]


def test_an_abandoned_stream_still_books(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _Ledger()
    _stub_ledger(monkeypatch, recorder)
    events = (
        b'data: {"model":"gpt-4o","choices":[{"delta":{"content":"h"}}]}\n\n'
        b'data: {"model":"gpt-4o","choices":[]}\n\n'
    )
    client, _ = _governed(
        lambda request: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=events
        )
    )
    with client.stream(
        "POST", "/v1/chat/completions", json={**_CHAT_BODY, "stream": True}
    ) as response:
        next(response.iter_bytes())  # read one chunk, then abandon

    assert len(recorder.captures) == 1
    # No usage was seen, so the authorized estimate is booked and flagged.
    assert "amount" not in recorder.captures[0]
    assert recorder.captures[0]["estimated"] is True


def test_a_gzipped_json_response_is_booked_and_stays_readable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "id": "chatcmpl-1",
        "model": "gpt-4o",
        "choices": [{"message": {"role": "assistant", "content": "hi"}}],
        "usage": _CHAT_USAGE,
    }

    def vendor(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "content-type": "application/json",
                "content-encoding": "gzip",
            },
            content=gzip.compress(json.dumps(payload).encode()),
        )

    recorder = _Ledger()
    _stub_ledger(monkeypatch, recorder)
    client, _ = _governed(vendor)

    response = client.post("/v1/chat/completions", json=_CHAT_BODY)

    assert response.json() == payload  # caller still reads the body fine
    assert len(recorder.captures) == 1
    assert recorder.captures[0]["tokens"] == 150


def test_embeddings_are_governed_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F-13: embeddings are real spend and were entirely invisible."""
    recorder = _Ledger()
    _stub_ledger(monkeypatch, recorder)

    def vendor(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [{"embedding": [0.1], "index": 0}],
                "model": "text-embedding-3-small",
                "usage": {"prompt_tokens": 8, "total_tokens": 8},
            },
        )

    client, _ = _governed(vendor)
    response = client.post(
        "/v1/embeddings",
        json={"model": "text-embedding-3-small", "input": "hello world"},
    )

    assert response.status_code == 200
    assert len(recorder.authorizes) == 1
    assert len(recorder.captures) == 1
    assert recorder.captures[0]["model"] == "text-embedding-3-small"
    assert recorder.captures[0]["tokens"] == 8


def test_the_responses_api_is_governed_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F-13: the Responses API is where OpenAI is steering new work."""
    recorder = _Ledger()
    _stub_ledger(monkeypatch, recorder)

    def vendor(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "resp_1",
                "model": "gpt-4o",
                "output": [{"type": "message", "content": []}],
                "usage": {"input_tokens": 36, "output_tokens": 87},
            },
        )

    client, _ = _governed(vendor)
    response = client.post("/v1/responses", json={"model": "gpt-4o", "input": "hi"})

    assert response.status_code == 200
    assert len(recorder.captures) == 1
    assert recorder.captures[0]["tokens"] == 123


def test_a_mismatched_vendor_path_follows_the_unmetered_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An anthropic-shaped path on an openai-vendor transport cannot be priced
    with this adapter — it follows the unmetered policy instead of guessing."""
    recorder = _Ledger()
    _stub_ledger(monkeypatch, recorder)
    client, _ = _governed(_chat_handler)
    response = client.post(
        "/v1/messages", json={"model": "claude-opus-5", "messages": []}
    )
    assert response.status_code == 403
    assert response.json()["error"]["type"] == "lucnt_unmetered_path"


# --- governed transport: async ----------------------------------------------------


def _agoverned(
    vendor_handler: Callable[[httpx.Request], httpx.Response],
) -> httpx.AsyncClient:
    transport = GovernedAsyncTransport(
        httpx.MockTransport(vendor_handler),
        OpenAIAdapter(),
        agent="a",
        entity="e",
        intent=None,
        project=None,
        unmetered="block",
    )
    return httpx.AsyncClient(transport=transport, base_url="https://api.openai.com")


def test_async_approved_call_is_authorized_then_settled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _Ledger()
    _stub_async_ledger(monkeypatch, recorder)

    async def main() -> httpx.Response:
        async with _agoverned(_chat_handler) as client:
            return await client.post("/v1/chat/completions", json=_CHAT_BODY)

    response = asyncio.run(main())
    assert response.status_code == 200
    assert len(recorder.authorizes) == 1
    assert len(recorder.captures) == 1
    assert recorder.captures[0]["tokens"] == 150


def test_async_blocked_call_never_reaches_the_vendor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _Ledger(status="blocked", code="budget_exhausted")
    _stub_async_ledger(monkeypatch, recorder)
    vendor_hits: list[str] = []

    def vendor(request: httpx.Request) -> httpx.Response:
        vendor_hits.append(request.url.path)
        return _chat_handler(request)

    async def main() -> httpx.Response:
        async with _agoverned(vendor) as client:
            return await client.post("/v1/chat/completions", json=_CHAT_BODY)

    response = asyncio.run(main())
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "budget_exhausted"
    assert vendor_hits == []


def test_async_streamed_response_books_from_the_scanner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _Ledger()
    _stub_async_ledger(monkeypatch, recorder)
    events = (
        b'data: {"model":"gpt-4o",'
        b'"usage":{"prompt_tokens":10,"completion_tokens":5}}\n\n'
        b"data: [DONE]\n\n"
    )

    def vendor(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=events
        )

    async def main() -> bytes:
        async with (
            _agoverned(vendor) as client,
            client.stream(
                "POST", "/v1/chat/completions", json={**_CHAT_BODY, "stream": True}
            ) as response,
        ):
            chunks = [chunk async for chunk in response.aiter_bytes()]
        return b"".join(chunks)

    received = asyncio.run(main())
    assert received == events
    assert len(recorder.captures) == 1
    assert recorder.captures[0]["tokens"] == 15


def test_async_wrapper_token_skips_the_net(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _Ledger()
    _stub_async_ledger(monkeypatch, recorder)

    async def main() -> httpx.Response:
        async with _agoverned(_chat_handler) as client:
            with wrapper_governed():
                return await client.post("/v1/chat/completions", json=_CHAT_BODY)

    response = asyncio.run(main())
    assert response.status_code == 200
    assert recorder.authorizes == []


# --- public constructors ------------------------------------------------------------


def test_metered_http_client_builds_a_governed_sync_client() -> None:
    client = metered_http_client(vendor="openai", agent="a", entity="e")
    assert isinstance(client, httpx.Client)
    assert isinstance(client._transport, GovernedTransport)
    client.close()


def test_metered_async_http_client_builds_a_governed_async_client() -> None:
    client = metered_async_http_client(vendor="anthropic", agent="a", entity="e")
    assert isinstance(client, httpx.AsyncClient)
    assert isinstance(client._transport, GovernedAsyncTransport)


def test_metered_http_client_rejects_an_unknown_vendor() -> None:
    with pytest.raises(LucntError, match="vendor"):
        metered_http_client(vendor="not-a-vendor", agent="a", entity="e")


def test_governor_module_exports_are_wired() -> None:
    import lucnt

    assert lucnt.metered_http_client is governor.metered_http_client
    assert lucnt.metered_async_http_client is governor.metered_async_http_client
