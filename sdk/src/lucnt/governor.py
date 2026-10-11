"""Transport-layer governor — every wire request is governed by construction.

`meter()`'s method wrappers cover the call paths the adapter *names*
(`chat.completions.create`, `messages.create/stream`). The finding was that a
real vendor client exposes many more paths that reach the network —
`with_raw_response`, `with_streaming_response`, `responses`, `beta.*`,
`embeddings` — and every one of them passed through the proxy silently
unmetered: a LangChain agent spent real money with no ledger row and no error.

This module closes that class of hole *structurally* rather than path-by-path:
both vendor SDKs funnel every request through one `httpx` transport, so a
governing transport wrapped around it sees every call a future SDK version can
invent. Each wire attempt is authorized before it is forwarded and captured
from the response's own usage after — the same authorize→capture primitive,
applied at the only chokepoint that cannot be bypassed from above.

**Blocked verdicts are synthetic 403 responses, never exceptions.** This is
forced, not stylistic, and was verified against the real `openai` client
(probe: 2026-07-28): both vendor SDKs wrap *any* exception raised below their
HTTP layer in their `APIConnectionError` and retry it, so raising
`PolicyBlocked` here would burn the client's retry budget re-asking policy and
then surface as a misleading connection error. A 403 response instead surfaces
as the vendor's own typed error (`PermissionDeniedError`), carries the lucnt
reason in its body, and is retried by neither SDK. Callers on the documented
`meter()` paths still get typed `PolicyBlocked` from the method wrappers, which
run *above* the vendor's HTTP layer; the transport is the safety net for every
path the wrappers do not name.

**Unknown spend paths fail closed.** A POST to a vendor path this module cannot
price (audio, images, batches, fine-tuning) is refused with a synthetic 403
naming the path and the escape hatches — `unmetered="allow"` to forward it
ungoverned (logged, once per path), or the explicit `authorize()`/`capture()`
primitive. Silent passthrough is the one behaviour this forbids.

Frameworks that build their own vendor client (LangChain, agents SDKs) are
covered via `metered_http_client()` / `metered_async_http_client()`: both
accept an `http_client` and every one of their requests then flows through this
governor:

    llm = ChatOpenAI(
        model="gpt-4o",
        http_client=lucnt.metered_http_client(vendor="openai", agent="researcher"),
        max_retries=0,
    )
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Callable, Coroutine, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from decimal import Decimal
from typing import Any, Literal, cast

import httpx

from lucnt.adapters.anthropic import AnthropicAdapter
from lucnt.adapters.base import _MILLION, MeterAdapter, Usage, _quantize
from lucnt.adapters.openai import OpenAIAdapter
from lucnt.exceptions import LucntError
from lucnt.ledger import (
    authorize,
    authorize_async,
    capture,
    capture_async,
    void,
    void_async,
)
from lucnt.models import Verdict
from lucnt.pricing import PriceInput

_logger = logging.getLogger("lucnt.governor")

Unmetered = Literal["block", "allow"]

# Set by meter()'s method wrappers around the vendor call they have already
# authorized, so the transport does not govern — and double-book — the same
# logical call. A contextvar so it is correct across threads and coroutines.
_WRAPPER_GOVERNED: ContextVar[bool] = ContextVar(
    "lucnt_wrapper_governed", default=False
)


@contextmanager
def wrapper_governed() -> Iterator[None]:
    """Mark the enclosed vendor call as already authorized by a meter() wrapper."""
    token = _WRAPPER_GOVERNED.set(True)
    try:
        yield
    finally:
        _WRAPPER_GOVERNED.reset(token)


# --------------------------------------------------------------------------- #
# endpoint classification
# --------------------------------------------------------------------------- #

_CHAT = "chat"
_LEGACY = "legacy"
_RESPONSES = "responses"
_EMBEDDINGS = "embeddings"
_MESSAGES = "messages"

# POST paths that are known token-priced spend, by suffix so custom base URLs
# (proxies, Azure-style gateways) still classify. Order matters: the most
# specific suffix wins, so `/chat/completions` is tested before `/completions`.
_KIND_BY_SUFFIX: tuple[tuple[str, str, str], ...] = (
    ("/chat/completions", "openai", _CHAT),
    ("/responses", "openai", _RESPONSES),
    ("/embeddings", "openai", _EMBEDDINGS),
    ("/messages/count_tokens", "anthropic", ""),  # free — handled below
    ("/messages", "anthropic", _MESSAGES),
    ("/completions", "openai", _LEGACY),
)

# POST paths that are known to be free of spend and safe to pass through.
_FREE_POST_SUFFIXES: tuple[str, ...] = ("/messages/count_tokens",)


def _classify(method: str, path: str) -> tuple[str, str] | None | Literal["unknown"]:
    """Map one request onto its governance route.

    Returns `None` to pass through (reads, and POSTs known to be free),
    `(vendor, kind)` for a priceable spend endpoint, and `"unknown"` for any
    other POST — which then follows the fail-closed unmetered policy. Only
    POST is governable: both vendors' spend endpoints are POST-only, and a
    GET/DELETE (list models, delete a file) moves no money.
    """
    if method.upper() != "POST":
        return None
    for suffix in _FREE_POST_SUFFIXES:
        if path.endswith(suffix):
            return None
    for suffix, vendor, kind in _KIND_BY_SUFFIX:
        if path.endswith(suffix):
            return (vendor, kind)
    return "unknown"


# --------------------------------------------------------------------------- #
# pre-call estimates per endpoint kind
# --------------------------------------------------------------------------- #


def _embedding_input_tokens(adapter: MeterAdapter, value: object) -> int:
    """Token estimate for an embeddings `input`, across its four wire shapes.

    `input` may be a string, a list of strings, a list of token ids, or a
    batch of token-id lists. Token ids are counted exactly (they *are* tokens);
    text uses the pack's chars-per-token heuristic like every other estimate.
    """
    if isinstance(value, str):
        return max(1, adapter._text_tokens(value))
    if isinstance(value, (list, tuple)):
        if all(isinstance(item, int) for item in value):
            return max(1, len(value))
        total = 0
        for item in value:
            if isinstance(item, str):
                total += adapter._text_tokens(item)
            elif isinstance(item, (list, tuple)):
                total += len(item)
            else:
                total += adapter._text_tokens(str(item))
        return max(1, total)
    return max(1, adapter._text_tokens(str(value)))


def _estimate(adapter: MeterAdapter, kind: str, body: Mapping[str, Any]) -> Decimal:
    """Pre-call cost estimate for one classified request body.

    Chat and Messages bodies are already the shape `MeterAdapter.estimate_cost`
    walks (model/messages/system/tools + output caps). The other kinds are
    normalised into that shape — or, for embeddings, priced directly as
    input-only — so every endpoint reuses the same structural walker and the
    same pricing table.

    Raises:
        LucntError: The body names no string model, or the model is unpriced —
            the caller refuses the request fail-closed rather than guessing.
    """
    if kind in (_CHAT, _MESSAGES):
        return adapter.estimate_cost(body)
    if kind == _LEGACY:
        prompt = body.get("prompt")
        texts = prompt if isinstance(prompt, (list, tuple)) else [prompt]
        pseudo: dict[str, Any] = {
            "model": body.get("model"),
            "messages": [{"role": "user", "content": str(t)} for t in texts],
        }
        if body.get("max_tokens") is not None:
            pseudo["max_tokens"] = body["max_tokens"]
        return adapter.estimate_cost(pseudo)
    if kind == _RESPONSES:
        items = body.get("input")
        messages: list[Any] = []
        if isinstance(items, str):
            messages.append({"role": "user", "content": items})
        elif isinstance(items, (list, tuple)):
            messages.extend(
                item if isinstance(item, Mapping) else {"content": str(item)}
                for item in items
            )
        instructions = body.get("instructions")
        if isinstance(instructions, str):
            messages.append({"role": "system", "content": instructions})
        pseudo = {"model": body.get("model"), "messages": messages}
        if body.get("max_output_tokens") is not None:
            pseudo["max_tokens"] = body["max_output_tokens"]
        return adapter.estimate_cost(pseudo)
    if kind == _EMBEDDINGS:
        model = body.get("model")
        if not isinstance(model, str):
            raise LucntError("embeddings request has no string 'model' to price")
        input_price, _output_price = adapter._prices(model)
        tokens = _embedding_input_tokens(adapter, body.get("input"))
        return _quantize(tokens * input_price / _MILLION)
    raise LucntError(f"no estimator for endpoint kind {kind!r}")  # unreachable


# --------------------------------------------------------------------------- #
# usage extraction from wire payloads
# --------------------------------------------------------------------------- #


def _usage_from_payload(kind: str, payload: Mapping[str, Any]) -> Usage:
    """Read normalized token usage off a parsed 2xx response body.

    Raises:
        LucntError: The payload carries no readable usage/model — the caller
            degrades to booking the authorized estimate, never raises on.
    """
    usage = payload.get("usage")
    model = payload.get("model")
    if not isinstance(usage, Mapping) or not isinstance(model, str):
        raise LucntError(f"{kind}: response carries no readable usage/model")
    try:
        if kind in (_CHAT, _LEGACY, _EMBEDDINGS):
            return Usage(
                model=model,
                input_tokens=int(usage["prompt_tokens"]),
                output_tokens=int(usage.get("completion_tokens", 0)),
            )
        return Usage(
            model=model,
            input_tokens=int(usage["input_tokens"]),
            output_tokens=int(usage["output_tokens"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise LucntError(f"{kind}: unreadable usage object ({exc!r})") from exc


class _SseUsageScanner:
    """Accumulate token usage out of a server-sent-event byte stream.

    One rule set reads all three dialects the governed endpoints speak: every
    `data:` event is probed at its top level, under `"message"` (Anthropic's
    `message_start`) and under `"response"` (the Responses API's
    `response.completed`) for a usage mapping and a model string. Token counts
    take the max seen — the vendors report cumulative totals, so the largest
    value is the final one regardless of which events carried it.
    """

    def __init__(self) -> None:
        self.input_tokens = 0
        self.output_tokens = 0
        self.model: str | None = None
        self.saw_usage = False
        self._buffer = b""

    def feed(self, chunk: bytes) -> None:
        """Consume one raw chunk, absorbing any completed `data:` lines."""
        self._buffer += chunk
        while b"\n" in self._buffer:
            line, self._buffer = self._buffer.split(b"\n", 1)
            self._feed_line(line.strip())

    def _feed_line(self, line: bytes) -> None:
        if not line.startswith(b"data:"):
            return
        try:
            event = json.loads(line[5:].strip())
        except ValueError:
            return  # e.g. the [DONE] sentinel
        if isinstance(event, dict):
            self._absorb(event)

    def _absorb(self, event: Mapping[str, Any]) -> None:
        for holder in (event, event.get("message"), event.get("response")):
            if not isinstance(holder, Mapping):
                continue
            model = holder.get("model")
            if isinstance(model, str):
                self.model = model
            usage = holder.get("usage")
            if isinstance(usage, Mapping):
                self._absorb_usage(usage)

    def _absorb_usage(self, usage: Mapping[str, Any]) -> None:
        pairs = (
            ("prompt_tokens", "input"),
            ("input_tokens", "input"),
            ("completion_tokens", "output"),
            ("output_tokens", "output"),
        )
        for key, side in pairs:
            value = usage.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                self.saw_usage = True
                if side == "input":
                    self.input_tokens = max(self.input_tokens, value)
                else:
                    self.output_tokens = max(self.output_tokens, value)


# --------------------------------------------------------------------------- #
# booking + release (best-effort — never raise after money moved)
# --------------------------------------------------------------------------- #


def _book_wire(
    adapter: MeterAdapter,
    verdict: Verdict,
    usage: Usage | None,
    requested_model: str | None,
) -> None:
    """Settle a governed spend, degrading exactly like `meter._book`."""
    if usage is not None:
        try:
            capture(
                verdict,
                amount=adapter.cost(usage, fallback_model=requested_model),
                model=usage.model,
                tokens=usage.input_tokens + usage.output_tokens,
            )
            return
        except Exception:
            _logger.exception(
                "lucnt: could not settle governed %s spend with actual usage "
                "(authorization_id=%s); falling back to booking the authorized "
                "estimate.",
                adapter.vendor,
                verdict.authorization_id,
            )
    else:
        _logger.warning(
            "lucnt: governed %s call carried no readable usage "
            "(authorization_id=%s); booking the authorized estimate.",
            adapter.vendor,
            verdict.authorization_id,
        )
    try:
        capture(verdict, estimated=True)
    except Exception:
        _logger.exception(
            "lucnt: governed %s spend is UNBOOKED and its hold is orphaned "
            "(authorization_id=%s). The vendor was paid; this transaction needs "
            "reconciliation.",
            adapter.vendor,
            verdict.authorization_id,
        )


async def _book_wire_async(
    adapter: MeterAdapter,
    verdict: Verdict,
    usage: Usage | None,
    requested_model: str | None,
) -> None:
    """Async twin of `_book_wire` — same degrade ladder, awaited."""
    if usage is not None:
        try:
            await capture_async(
                verdict,
                amount=adapter.cost(usage, fallback_model=requested_model),
                model=usage.model,
                tokens=usage.input_tokens + usage.output_tokens,
            )
            return
        except Exception:
            _logger.exception(
                "lucnt: could not settle governed %s spend with actual usage "
                "(authorization_id=%s); falling back to booking the authorized "
                "estimate.",
                adapter.vendor,
                verdict.authorization_id,
            )
    else:
        _logger.warning(
            "lucnt: governed %s call carried no readable usage "
            "(authorization_id=%s); booking the authorized estimate.",
            adapter.vendor,
            verdict.authorization_id,
        )
    try:
        await capture_async(verdict, estimated=True)
    except Exception:
        _logger.exception(
            "lucnt: governed %s spend is UNBOOKED and its hold is orphaned "
            "(authorization_id=%s). The vendor was paid; this transaction needs "
            "reconciliation.",
            adapter.vendor,
            verdict.authorization_id,
        )


def _release(verdict: Verdict, reason: str) -> None:
    """Void a hold whose wire attempt failed, never raising."""
    try:
        void(verdict, reason=reason)
    except Exception:
        _logger.warning(
            "lucnt: could not release the hold for %s (authorization_id=%s); "
            "the server-side TTL bounds the stale reservation.",
            reason,
            verdict.authorization_id,
            exc_info=True,
        )


async def _release_async(verdict: Verdict, reason: str) -> None:
    """Async twin of `_release`."""
    try:
        await void_async(verdict, reason=reason)
    except Exception:
        _logger.warning(
            "lucnt: could not release the hold for %s (authorization_id=%s); "
            "the server-side TTL bounds the stale reservation.",
            reason,
            verdict.authorization_id,
            exc_info=True,
        )


# --------------------------------------------------------------------------- #
# synthetic refusals
# --------------------------------------------------------------------------- #


def _refusal(
    request: httpx.Request, error_type: str, code: str, message: str
) -> httpx.Response:
    """A synthetic 403 the vendor SDK surfaces typed, loud, and unretried.

    403 specifically: verified against the real clients — neither vendor SDK
    retries it (unlike 409/429/5xx), and both raise their PermissionDenied
    error carrying this body's message.
    """
    return httpx.Response(
        403,
        headers={"x-lucnt-blocked": code},
        json={"error": {"type": error_type, "code": code, "message": message}},
        request=request,
    )


def _blocked_message(vendor: str, verdict: Verdict) -> str:
    return (
        f"lucnt blocked this call before it reached {vendor}: "
        f"{verdict.reason or verdict.code}. No money moved. "
        f"(authorization_id={verdict.authorization_id})"
    )


def _unpriceable_message(vendor: str, exc: Exception) -> str:
    return (
        f"lucnt could not price this {vendor} call, so it was refused before "
        f"any money moved ({exc}). Add the model via meter(pricing=...) or "
        f"metered_http_client(pricing=...), or govern the spend explicitly "
        f"with lucnt.authorize()/capture()."
    )


def _unmetered_message(path: str) -> str:
    return (
        f"lucnt cannot meter POST {path} and refused it before any money "
        f'moved (fail-closed). Pass unmetered="allow" to forward such calls '
        f"ungoverned, or govern the spend explicitly with "
        f"lucnt.authorize()/capture()."
    )


# --------------------------------------------------------------------------- #
# governed byte streams (usage arrives last on a streamed response)
# --------------------------------------------------------------------------- #


class _GovernedByteStream(httpx.SyncByteStream):
    """Pass a streamed body through, booking exactly once when it ends.

    Books on every exit path — exhaustion, mid-stream error, and an abandoned
    stream whose `close()` arrives first — because the vendor generated (and
    charged for) whatever was streamed before the exit.
    """

    def __init__(
        self,
        inner: httpx.SyncByteStream,
        scanner: _SseUsageScanner | None,
        book: Callable[[_SseUsageScanner | None], None],
    ) -> None:
        self._inner = inner
        self._scanner = scanner
        self._book = book
        self._booked = False

    def __iter__(self) -> Iterator[bytes]:
        try:
            for chunk in self._inner:
                if self._scanner is not None:
                    self._scanner.feed(chunk)
                yield chunk
        finally:
            self._book_once()

    def _book_once(self) -> None:
        if self._booked:
            return
        self._booked = True
        self._book(self._scanner)

    def close(self) -> None:
        try:
            self._inner.close()
        finally:
            self._book_once()


class _GovernedAsyncByteStream(httpx.AsyncByteStream):
    """Async twin of `_GovernedByteStream` — books once on every exit path."""

    def __init__(
        self,
        inner: httpx.AsyncByteStream,
        scanner: _SseUsageScanner | None,
        book: Callable[[_SseUsageScanner | None], Coroutine[Any, Any, None]],
    ) -> None:
        self._inner = inner
        self._scanner = scanner
        self._book = book
        self._booked = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        try:
            async for chunk in self._inner:
                if self._scanner is not None:
                    self._scanner.feed(chunk)
                yield chunk
        finally:
            await self._book_once()

    async def _book_once(self) -> None:
        if self._booked:
            return
        self._booked = True
        await self._book(self._scanner)

    async def aclose(self) -> None:
        try:
            await self._inner.aclose()
        finally:
            await self._book_once()


# --------------------------------------------------------------------------- #
# the transports
# --------------------------------------------------------------------------- #


def _scanner_usage(
    scanner: _SseUsageScanner | None, requested_model: str | None
) -> Usage | None:
    """Convert final scanner state into a `Usage`, or None when nothing usable."""
    if scanner is None or not scanner.saw_usage:
        return None
    model = scanner.model or requested_model
    if model is None:
        return None
    return Usage(
        model=model,
        input_tokens=scanner.input_tokens,
        output_tokens=scanner.output_tokens,
    )


class _GovernorCore:
    """State and pure decision logic shared by the sync and async transports.

    The two transports differ only in how they do I/O — the classification,
    estimation, refusal shapes, and unmetered policy must be identical, so
    they live here once (the same reasoning as ledger's `_Call`).
    """

    def __init__(
        self,
        adapter: MeterAdapter,
        *,
        agent: str | None,
        entity: str | None,
        intent: str | None,
        project: str | None,
        unmetered: Unmetered,
    ) -> None:
        if unmetered not in ("block", "allow"):
            raise LucntError(f'unmetered must be "block" or "allow", got {unmetered!r}')
        self._adapter = adapter
        self._agent = agent
        self._entity = entity
        self._intent = intent
        self._project = project
        self._unmetered = unmetered
        self._warned_paths: set[str] = set()

    def _plan(
        self, request: httpx.Request
    ) -> httpx.Response | tuple[str, str | None, Decimal, bool] | None:
        """Decide one request's fate before any I/O happens.

        Returns None to pass through, a synthetic refusal `Response` to return
        as-is, or `(kind, model, estimate, streaming)` to govern.
        """
        if _WRAPPER_GOVERNED.get():
            return None
        route = _classify(request.method, request.url.path)
        if route is None:
            return None
        if route == "unknown" or route[0] != self._adapter.vendor:
            return self._handle_unmetered(request)
        kind = route[1]
        try:
            parsed = json.loads(request.read() or b"{}")
            if not isinstance(parsed, dict):
                raise LucntError("request body is not a JSON object")
            amount = _estimate(self._adapter, kind, parsed)
        except Exception as exc:
            return _refusal(
                request,
                "lucnt_unpriceable",
                "unpriceable",
                _unpriceable_message(self._adapter.vendor, exc),
            )
        model = parsed.get("model")
        return (
            kind,
            model if isinstance(model, str) else None,
            amount,
            parsed.get("stream") is True,
        )

    def _handle_unmetered(self, request: httpx.Request) -> httpx.Response | None:
        path = request.url.path
        if self._unmetered == "allow":
            if path not in self._warned_paths:
                self._warned_paths.add(path)
                _logger.warning(
                    'lucnt: POST %s is not meterable and unmetered="allow" is '
                    "set — forwarding it UNGOVERNED. This spend will have no "
                    "ledger row.",
                    path,
                )
            return None
        return _refusal(
            request, "lucnt_unmetered_path", "unmetered_path", _unmetered_message(path)
        )

    def _reason(self, kind: str, model: str | None) -> str:
        return f"{self._adapter.vendor} {kind} ({model})"


class GovernedTransport(httpx.BaseTransport):
    """An `httpx` transport that authorizes, forwards, then books every spend.

    Wraps the vendor client's real transport; installed by `meter()` or built
    standalone via `metered_http_client()`. Per wire attempt: authorize (a
    block becomes a synthetic 403, see module docstring) → forward → on a 2xx,
    settle from the response's usage (streamed responses book when the stream
    ends); on a non-2xx or a wire error, release the hold — no money moved.
    """

    def __init__(
        self,
        inner: httpx.BaseTransport,
        adapter: MeterAdapter,
        *,
        agent: str | None = None,
        entity: str | None = None,
        intent: str | None = None,
        project: str | None = None,
        unmetered: Unmetered = "block",
    ) -> None:
        """Wrap `inner`; decisions are shared with the async twin via the core."""
        self._inner = inner
        self._core = _GovernorCore(
            adapter,
            agent=agent,
            entity=entity,
            intent=intent,
            project=project,
            unmetered=unmetered,
        )

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        """Authorize → forward → book (or refuse/release), per wire attempt."""
        plan = self._core._plan(request)
        if plan is None:
            return self._inner.handle_request(request)
        if isinstance(plan, httpx.Response):
            return plan
        kind, model, amount, streaming = plan
        core = self._core
        adapter = core._adapter
        verdict = authorize(
            vendor=adapter.vendor,
            amount=amount,
            currency=adapter.currency,
            reason=core._reason(kind, model),
            agent=core._agent,
            entity=core._entity,
            intent=core._intent,
            project=core._project,
            model=model,
            strict=False,
        )
        if not verdict.approved:
            return _refusal(
                request,
                "lucnt_policy_blocked",
                verdict.code,
                _blocked_message(adapter.vendor, verdict),
            )
        try:
            response = self._inner.handle_request(request)
        except BaseException as exc:
            _release(verdict, f"{type(exc).__name__} on the wire to {adapter.vendor}")
            raise
        if not 200 <= response.status_code < 300:
            _release(verdict, f"{adapter.vendor} returned HTTP {response.status_code}")
            return response
        content_type = response.headers.get("content-type", "")
        if streaming or content_type.startswith("text/event-stream"):
            # Usage arrives last on a stream; scan it in flight and book when
            # the caller finishes (or abandons) the stream. A compressed SSE
            # body cannot be scanned — the estimate is booked instead.
            encoded = response.headers.get("content-encoding", "identity") != "identity"
            scanner = None if encoded else _SseUsageScanner()

            def book(final: _SseUsageScanner | None) -> None:
                _book_wire(adapter, verdict, _scanner_usage(final, model), model)

            return httpx.Response(
                response.status_code,
                headers=response.headers,
                stream=_GovernedByteStream(
                    cast("httpx.SyncByteStream", response.stream), scanner, book
                ),
                extensions=response.extensions,
                request=request,
            )
        # Buffer the raw (possibly compressed) bytes and hand the caller an
        # identical response; parse usage from the rebuilt copy, which decodes
        # content-encoding through httpx's own machinery.
        raw = b"".join(cast("httpx.SyncByteStream", response.stream))
        response.close()
        rebuilt = httpx.Response(
            response.status_code,
            headers=response.headers,
            content=raw,
            extensions=response.extensions,
            request=request,
        )
        usage: Usage | None = None
        try:
            usage = _usage_from_payload(kind, rebuilt.json())
        except Exception:
            usage = None  # degrade to the estimate; _book_wire logs it
        _book_wire(adapter, verdict, usage, model)
        return rebuilt

    def close(self) -> None:
        """Close the wrapped transport."""
        self._inner.close()


class GovernedAsyncTransport(httpx.AsyncBaseTransport):
    """Async twin of `GovernedTransport` — same decisions, awaited I/O."""

    def __init__(
        self,
        inner: httpx.AsyncBaseTransport,
        adapter: MeterAdapter,
        *,
        agent: str | None = None,
        entity: str | None = None,
        intent: str | None = None,
        project: str | None = None,
        unmetered: Unmetered = "block",
    ) -> None:
        """Wrap `inner`; decisions are shared with the sync twin via the core."""
        self._inner = inner
        self._core = _GovernorCore(
            adapter,
            agent=agent,
            entity=entity,
            intent=intent,
            project=project,
            unmetered=unmetered,
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """Authorize → forward → book (or refuse/release), per wire attempt."""
        plan = self._core._plan(request)
        if plan is None:
            return await self._inner.handle_async_request(request)
        if isinstance(plan, httpx.Response):
            return plan
        kind, model, amount, streaming = plan
        core = self._core
        adapter = core._adapter
        verdict = await authorize_async(
            vendor=adapter.vendor,
            amount=amount,
            currency=adapter.currency,
            reason=core._reason(kind, model),
            agent=core._agent,
            entity=core._entity,
            intent=core._intent,
            project=core._project,
            model=model,
            strict=False,
        )
        if not verdict.approved:
            return _refusal(
                request,
                "lucnt_policy_blocked",
                verdict.code,
                _blocked_message(adapter.vendor, verdict),
            )
        try:
            response = await self._inner.handle_async_request(request)
        except BaseException as exc:
            await _release_async(
                verdict, f"{type(exc).__name__} on the wire to {adapter.vendor}"
            )
            raise
        if not 200 <= response.status_code < 300:
            await _release_async(
                verdict, f"{adapter.vendor} returned HTTP {response.status_code}"
            )
            return response
        content_type = response.headers.get("content-type", "")
        if streaming or content_type.startswith("text/event-stream"):
            encoded = response.headers.get("content-encoding", "identity") != "identity"
            scanner = None if encoded else _SseUsageScanner()

            async def book(final: _SseUsageScanner | None) -> None:
                await _book_wire_async(
                    adapter, verdict, _scanner_usage(final, model), model
                )

            return httpx.Response(
                response.status_code,
                headers=response.headers,
                stream=_GovernedAsyncByteStream(
                    cast("httpx.AsyncByteStream", response.stream), scanner, book
                ),
                extensions=response.extensions,
                request=request,
            )
        raw = b"".join(
            [chunk async for chunk in cast("httpx.AsyncByteStream", response.stream)]
        )
        await response.aclose()
        rebuilt = httpx.Response(
            response.status_code,
            headers=response.headers,
            content=raw,
            extensions=response.extensions,
            request=request,
        )
        usage: Usage | None = None
        try:
            usage = _usage_from_payload(kind, rebuilt.json())
        except Exception:
            usage = None
        await _book_wire_async(adapter, verdict, usage, model)
        return rebuilt

    async def aclose(self) -> None:
        """Close the wrapped transport."""
        await self._inner.aclose()


# --------------------------------------------------------------------------- #
# public constructors + meter() install
# --------------------------------------------------------------------------- #

_ADAPTERS_BY_VENDOR: dict[str, type[MeterAdapter]] = {
    "openai": OpenAIAdapter,
    "anthropic": AnthropicAdapter,
}


def _adapter_for(
    vendor: str, pricing: Mapping[str, tuple[PriceInput, PriceInput]] | None
) -> MeterAdapter:
    adapter_type = _ADAPTERS_BY_VENDOR.get(vendor)
    if adapter_type is None:
        raise LucntError(
            f"unknown vendor {vendor!r} (known: {sorted(_ADAPTERS_BY_VENDOR)})"
        )
    return adapter_type(pricing=pricing)


def metered_http_client(
    *,
    vendor: str,
    agent: str | None = None,
    entity: str | None = None,
    intent: str | None = None,
    project: str | None = None,
    pricing: Mapping[str, tuple[PriceInput, PriceInput]] | None = None,
    unmetered: Unmetered = "block",
    **httpx_kwargs: Any,
) -> httpx.Client:
    """Build an `httpx.Client` whose every request is governed.

    The integration point for frameworks that construct their own vendor
    client: anything accepting an `http_client=` — LangChain's `ChatOpenAI`,
    the vendor SDKs themselves — is fully governed by passing one of these,
    with no other code change:

        llm = ChatOpenAI(
            model="gpt-4o",
            http_client=lucnt.metered_http_client(vendor="openai", agent="researcher"),
            max_retries=0,
        )

    Semantics per request: known spend endpoints are authorized before the
    request is forwarded and settled from the response's usage after (streamed
    responses settle when the stream ends). A policy block or an unpriceable
    call is refused with a synthetic 403 carrying the reason — surfaced by the
    vendor SDK as its own typed error, never retried, no money moved. Reads
    pass through untouched. Unknown POST paths follow `unmetered`.

    **Turn off the vendor SDK's own retries** (`max_retries=0`) and retry at
    the lucnt layer instead — the same rule as `meter()`.

    Args:
        vendor: `"openai"` or `"anthropic"` — which wire shapes to expect.
        agent: Agent handle to attribute spend to. Falls back to `task()`.
        entity: Legal entity handle. Falls back to `task()`.
        intent: Free-text purpose. Falls back to `task()`.
        project: Cost-center label. Falls back to `task()`.
        pricing: Per-model `(input, output)` price per 1M tokens, merged over
            the packaged pack (caller wins). `float` is rejected.
        unmetered: `"block"` (default) refuses unpriceable POST paths with a
            synthetic 403; `"allow"` forwards them ungoverned, logged once per
            path.
        **httpx_kwargs: Forwarded to `httpx.Client` (timeouts, proxies, ...).

    Returns:
        A ready `httpx.Client` with the governor installed as its transport.

    Raises:
        LucntError: Unknown `vendor`, malformed `pricing`, or a bad `unmetered`.
    """
    adapter = _adapter_for(vendor, pricing)
    transport = GovernedTransport(
        httpx.HTTPTransport(),
        adapter,
        agent=agent,
        entity=entity,
        intent=intent,
        project=project,
        unmetered=unmetered,
    )
    return httpx.Client(transport=transport, **httpx_kwargs)


def metered_async_http_client(
    *,
    vendor: str,
    agent: str | None = None,
    entity: str | None = None,
    intent: str | None = None,
    project: str | None = None,
    pricing: Mapping[str, tuple[PriceInput, PriceInput]] | None = None,
    unmetered: Unmetered = "block",
    **httpx_kwargs: Any,
) -> httpx.AsyncClient:
    """Build a governed `httpx.AsyncClient`.

    The async twin of `metered_http_client()`; see there for semantics and
    arguments. Pass it as the async `http_client` (LangChain:
    `http_async_client=`).
    """
    adapter = _adapter_for(vendor, pricing)
    transport = GovernedAsyncTransport(
        httpx.AsyncHTTPTransport(),
        adapter,
        agent=agent,
        entity=entity,
        intent=intent,
        project=project,
        unmetered=unmetered,
    )
    return httpx.AsyncClient(transport=transport, **httpx_kwargs)


def install_governor(
    client: object,
    adapter: MeterAdapter,
    *,
    agent: str | None,
    entity: str | None,
    intent: str | None,
    project: str | None,
    unmetered: Unmetered,
) -> None:
    """Swap a vendor client's httpx transport for a governed one (in place).

    Called by `meter()`. Mutating rather than copying is deliberate: the
    governance must hold even for code that kept a reference to the unwrapped
    client — the exact failure this closes.

    Raises:
        LucntError: `client` exposes no httpx client at `._client`. Failing
            loud is the point — proceeding would silently leave every
            non-wrapped call path unmetered, which is the bug this module
            exists to close. (Both real vendor SDKs are httpx-based and expose
            `._client`; an object that does not is either a test double that
            should model reality, or an HTTP stack this SDK cannot govern.)
    """
    http_client = getattr(client, "_client", None)
    if isinstance(http_client, httpx.AsyncClient):
        inner: Any = http_client._transport
        if isinstance(inner, GovernedAsyncTransport):
            inner = inner._inner  # re-metering replaces, never stacks
        http_client._transport = GovernedAsyncTransport(
            inner,
            adapter,
            agent=agent,
            entity=entity,
            intent=intent,
            project=project,
            unmetered=unmetered,
        )
        return
    if isinstance(http_client, httpx.Client):
        sync_inner: Any = http_client._transport
        if isinstance(sync_inner, GovernedTransport):
            sync_inner = sync_inner._inner
        http_client._transport = GovernedTransport(
            sync_inner,
            adapter,
            agent=agent,
            entity=entity,
            intent=intent,
            project=project,
            unmetered=unmetered,
        )
        return
    raise LucntError(
        f"meter() cannot install the transport governor on "
        f"{type(client).__module__}.{type(client).__qualname__}: expected an "
        f"httpx.Client or httpx.AsyncClient at `._client`, found "
        f"{type(http_client).__name__}. Without the governor, call paths "
        f"outside the wrapped methods would reach the vendor unmetered."
    )
