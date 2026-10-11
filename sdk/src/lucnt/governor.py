"""Transport-layer governor: every vendor wire request is authorized and captured.

Blocks are synthetic 403 responses, not exceptions: vendor SDKs retry exceptions
raised below their HTTP layer but never retry a 403. Unpriceable POSTs fail closed.
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

# Set by meter() wrappers so the transport doesn't double-book the same call.
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

# Matched by suffix so custom base URLs still classify; most specific first.
_KIND_BY_SUFFIX: tuple[tuple[str, str, str], ...] = (
    ("/chat/completions", "openai", _CHAT),
    ("/responses", "openai", _RESPONSES),
    ("/embeddings", "openai", _EMBEDDINGS),
    ("/messages/count_tokens", "anthropic", ""),  # free — handled below
    ("/messages", "anthropic", _MESSAGES),
    ("/completions", "openai", _LEGACY),
)

_FREE_POST_SUFFIXES: tuple[str, ...] = ("/messages/count_tokens",)


def _classify(method: str, path: str) -> tuple[str, str] | None | Literal["unknown"]:
    """Map a request to None (pass through), `(vendor, kind)`, or `"unknown"`.

    Only POST can spend; an unknown POST follows the fail-closed policy.
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
    """Token estimate for embeddings `input` (str, strs, token ids, or batches)."""
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
    """Pre-call cost estimate, normalising each kind into the chat-shaped walker.

    Raises:
        LucntError: No string model, or the model is unpriced (caller fails closed).
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
    """Read normalized token usage off a parsed 2xx response body."""
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

    Probes each event's top level, `message`, and `response`. Counts take the
    max seen, since vendors report cumulative totals.
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
    """A synthetic 403: neither vendor SDK retries it, and both raise it typed."""
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
    """Pass a streamed body through, booking exactly once on any exit path.

    The vendor charges for whatever streamed before an error or abandonment.
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
    """Decision logic shared by the sync and async transports, so they can't drift."""

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
        """Return None (pass), a refusal, or `(kind, model, estimate, streaming)`."""
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

    A non-2xx or wire error releases the hold; streams book when they end.
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
        """Wrap `inner` with the governor."""
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
            # Compressed SSE can't be scanned; the estimate is booked instead.
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
        # Rebuild from raw bytes so httpx decodes content-encoding for parsing.
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
        """Wrap `inner` with the governor."""
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

    Pass it as `http_client=` to frameworks that build their own vendor client
    (e.g. LangChain), and set the vendor client's `max_retries=0`. Blocks and
    unpriceable calls surface as a vendor 403 error; no money moves.

    Args:
        vendor: `"openai"` or `"anthropic"`.
        agent: Agent handle; falls back to `task()`.
        entity: Legal entity handle; falls back to `task()`.
        intent: Free-text purpose; falls back to `task()`.
        project: Cost-center label; falls back to `task()`.
        pricing: Model id -> `(input, output)` per 1M tokens; wins over the pack.
        unmetered: `"block"` refuses unknown POST paths; `"allow"` forwards them.
        **httpx_kwargs: Forwarded to `httpx.Client`.

    Returns:
        An `httpx.Client` with the governor as its transport.

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
    """Async `metered_http_client()`; same arguments, returns an `httpx.AsyncClient`."""
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
    """Swap a vendor client's httpx transport for a governed one, in place.

    In place so references to the unwrapped client are governed too.

    Raises:
        LucntError: No httpx client at `._client`; proceeding would leave
            non-wrapped paths unmetered.
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
