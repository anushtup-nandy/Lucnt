"""meter(client) — auto-instrument an LLM client's spend (Phase 3.5 / B).

`meter()` wraps a customer's OpenAI/Anthropic client and returns a proxy that
behaves identically, except the completion call is governed: it authorizes a
pre-call cost *estimate* (blocking before the request if policy says no), makes
the real call, then captures the *actual* cost read from the response's usage.
It is a pure convenience layer over the P1 `authorize`/`capture` primitive — no
new backend surface.

    client = lucnt.meter(OpenAI(), agent="research", entity="in")
    resp = client.chat.completions.create(model="gpt-4o", messages=[...])
    # ^ authorized before the call, captured (with real cost/model/tokens) after

A blocking verdict **raises** (`PolicyBlocked` / `BudgetExhausted`) rather than
returning — the wrapped method must yield the vendor's response type or nothing,
so there is no falsy-return option.

**Sync and async clients are both supported** (T5-6): `meter()` detects which it
was handed and installs the matching wrapper, so the same call governs `OpenAI()`
and `AsyncOpenAI()`. The async wrappers mirror the sync ones exactly, including
the best-effort-bookkeeping rule below. Streaming *context managers* authorize
on entry on both paths (F-11): the async one because `async with` gives the
wrapper no earlier point at which it may await, the sync one to match — so a
manager built and never entered mints no hold on either path.

**The asymmetry that governs this module:** raising *before* the vendor call is
correct — that is the policy gate doing its job, and no money has moved. Raising
*after* it is never acceptable, because the customer has already paid and the
completion is the thing they bought. So post-call bookkeeping is best-effort and
logged, never fatal (T5-1); see `_book`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any, TypeVar, cast

from lucnt.adapters import MeterAdapter, resolve_adapter
from lucnt.exceptions import LucntError
from lucnt.governor import Unmetered, install_governor, wrapper_governed
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

ClientT = TypeVar("ClientT")

# Library convention: log to a named logger and add no handler, so the host
# application's logging configuration decides where this goes.
_logger = logging.getLogger("lucnt.meter")


class _Patched:
    """Transparent proxy over one object, overriding a single named attribute.

    Delegates every attribute except those in `overrides` to the wrapped
    target, so a metered client behaves exactly like the real one apart from
    the one intercepted method.
    """

    def __init__(self, target: object, overrides: dict[str, object]) -> None:
        """Store the wrapped target and the attribute overrides."""
        object.__setattr__(self, "_target", target)
        object.__setattr__(self, "_overrides", overrides)

    def __getattr__(self, name: str) -> Any:
        """Return an override if present, else delegate to the wrapped target."""
        overrides: dict[str, object] = object.__getattribute__(self, "_overrides")
        if name in overrides:
            return overrides[name]
        return getattr(object.__getattribute__(self, "_target"), name)


def _patch_paths(root: object, overrides: Mapping[tuple[str, ...], object]) -> _Patched:
    """Proxy `root` so each attribute chain in `overrides` resolves to its value.

    Takes every path at once rather than one at a time because sibling
    overrides share a parent: Anthropic needs both `messages.create` and
    `messages.stream` replaced (T5-5), and patching them independently would
    build two proxies over `messages`, the second discarding the first.
    """
    direct: dict[str, object] = {}
    nested: dict[str, dict[tuple[str, ...], object]] = {}
    for path, replacement in overrides.items():
        head, *rest = path
        if rest:
            nested.setdefault(head, {})[tuple(rest)] = replacement
        else:
            direct[head] = replacement
    for head, sub_overrides in nested.items():
        if head in direct:  # an override at this level already covers the branch
            continue
        direct[head] = _patch_paths(getattr(root, head), sub_overrides)
    return _Patched(root, direct)


def _make_metered_call(
    adapter: MeterAdapter,
    original: Callable[..., Any],
    *,
    agent: str | None,
    entity: str | None,
    intent: str | None,
    project: str | None,
) -> Callable[..., Any]:
    """Wrap the vendor's completion call with authorize→call→capture."""

    def metered_call(*args: Any, **kwargs: Any) -> Any:
        model = kwargs.get("model")
        streaming = adapter.is_streaming_request(kwargs)
        # Authorize a pre-call estimate; strict=True raises if policy blocks,
        # so the real call below never runs on a blocked spend. Raising *here*
        # is the gate working — no money has moved yet.
        verdict = authorize(
            vendor=adapter.vendor,
            amount=adapter.estimate_cost(kwargs),
            # The vendor's billing currency, from the data pack — never a
            # hardcoded "USD" (C-6).
            currency=adapter.currency,
            reason=f"{adapter.vendor} completion ({model})",
            agent=agent,
            entity=entity,
            intent=intent,
            project=project,
            model=model if isinstance(model, str) else None,
            strict=True,
        )
        try:
            # The wrapper has already authorized this logical call; the token
            # tells the transport governor not to govern it a second time.
            with wrapper_governed():
                response = original(*args, **kwargs)
        except BaseException as exc:
            # The vendor call itself failed, so no money moved — but the hold is
            # still reserving the agent's budget and would do so forever (T5-4).
            # Release it, then re-raise the vendor's error untouched: that is the
            # information the caller needs, and it must not be replaced by
            # anything that goes wrong while cleaning up.
            _release(verdict, f"{type(exc).__name__} from {adapter.vendor} call")
            raise
        # ---------------------------------------------------------------------
        # The vendor has now been paid. Everything below is bookkeeping, and
        # bookkeeping must never destroy what the customer paid for (T5-1).
        # ---------------------------------------------------------------------
        if streaming:
            # A streamed response is an iterator whose usage arrives last, so
            # booking has to wait until the caller has consumed it (T5-5).
            return _metered_stream(adapter, verdict, response, requested_model=model)
        _book(adapter, verdict, response, requested_model=model)
        return response

    return metered_call


def _make_metered_stream_call(
    adapter: MeterAdapter,
    original: Callable[..., Any],
    *,
    agent: str | None,
    entity: str | None,
    intent: str | None,
    project: str | None,
) -> Callable[..., Any]:
    """Wrap a vendor streaming *helper* (Anthropic `messages.stream`) — T5-5.

    Returns the manager without authorizing (F-11): the hold is taken in
    `__enter__`, mirroring the async twin, so a manager that is built and never
    entered mints no hold at all. The previous shape authorized eagerly here,
    which left `mgr = client.messages.stream(...)` without a `with` reserving
    budget until the 24h server-side TTL released it.
    """

    def metered_stream_call(*args: Any, **kwargs: Any) -> Any:
        return _MeteredStreamManager(
            lambda: original(*args, **kwargs),
            adapter,
            # The whole request, not just the model: the estimate is computed
            # from the prompt's size, so passing only the model would price the
            # hold as if the request were empty and under-reserve every time.
            kwargs,
            agent=agent,
            entity=entity,
            intent=intent,
            project=project,
        )

    return metered_stream_call


def _make_async_metered_call(
    adapter: MeterAdapter,
    original: Callable[..., Any],
    *,
    agent: str | None,
    entity: str | None,
    intent: str | None,
    project: str | None,
) -> Callable[..., Any]:
    """Wrap an async vendor completion call with authorize→call→capture (T5-6).

    Line-for-line the same shape as `_make_metered_call`, awaited. The
    asymmetry that governs the sync version governs this one identically:
    raising before `original(...)` is the gate working, raising after it would
    destroy a completion the customer has already paid for.
    """

    async def metered_call(*args: Any, **kwargs: Any) -> Any:
        model = kwargs.get("model")
        streaming = adapter.is_streaming_request(kwargs)
        verdict = await authorize_async(
            vendor=adapter.vendor,
            amount=adapter.estimate_cost(kwargs),
            currency=adapter.currency,
            reason=f"{adapter.vendor} completion ({model})",
            agent=agent,
            entity=entity,
            intent=intent,
            project=project,
            model=model if isinstance(model, str) else None,
            strict=True,
        )
        try:
            with wrapper_governed():
                response = await original(*args, **kwargs)
        except BaseException as exc:
            await _release_async(
                verdict, f"{type(exc).__name__} from {adapter.vendor} call"
            )
            raise
        # ---------------------------------------------------------------------
        # The vendor has now been paid. Everything below is bookkeeping, and
        # bookkeeping must never destroy what the customer paid for (T5-1).
        # ---------------------------------------------------------------------
        if streaming:
            return _metered_astream(
                adapter, verdict, response, requested_model=model
            )
        await _book_async(adapter, verdict, response, requested_model=model)
        return response

    return metered_call


def _make_async_metered_stream_call(
    adapter: MeterAdapter,
    original: Callable[..., Any],
    *,
    agent: str | None,
    entity: str | None,
    intent: str | None,
    project: str | None,
) -> Callable[..., Any]:
    """Wrap an async vendor streaming *helper* (`AsyncAnthropic.messages.stream`).

    **Deliberately not a coroutine function.** `async with client.messages.stream(...)`
    calls this and then looks for `__aenter__` on what comes back; a coroutine
    object has none, so making this `async def` would break every caller's
    `async with`. It returns the manager synchronously instead, and the
    authorize happens inside `__aenter__` — the first point at which the wrapper
    is allowed to await.
    """

    def metered_stream_call(*args: Any, **kwargs: Any) -> Any:
        return _MeteredAsyncStreamManager(
            lambda: original(*args, **kwargs),
            adapter,
            # The whole request, not just the model: the estimate is computed
            # from the prompt's size, so passing only the model would price the
            # hold as if the request were empty and under-reserve every time.
            kwargs,
            agent=agent,
            entity=entity,
            intent=intent,
            project=project,
        )

    return metered_stream_call


def _usage_bearing(adapter: MeterAdapter, chunk: object) -> bool:
    """True if `chunk` carries readable token usage."""
    try:
        adapter.usage_from_response(chunk)
    except Exception:
        return False
    return True


def _metered_stream(
    adapter: MeterAdapter,
    verdict: Verdict,
    stream: Any,
    *,
    requested_model: object,
) -> Any:
    """Pass a streamed response through, booking once the stream ends (T5-5).

    Usage arrives on the *last* chunk (OpenAI sends it only with
    `stream_options={"include_usage": True}`), so the actual cost is unknown
    until the caller has consumed everything. This yields each chunk straight
    through and books afterwards.

    Books on *every* exit path, including a mid-stream error and the caller
    breaking out of the loop early (which reaches this as `GeneratorExit`): the
    vendor generated — and charged for — whatever was streamed before the exit,
    so that spend belongs in the ledger. This is why an abandoned stream is
    never voided: unlike a failed call, tokens really were consumed.
    """
    last_usage_chunk: object | None = None
    try:
        for chunk in stream:
            if _usage_bearing(adapter, chunk):
                last_usage_chunk = chunk
            yield chunk
    except BaseException:
        _book_streamed(adapter, verdict, last_usage_chunk, requested_model)
        raise
    _book_streamed(adapter, verdict, last_usage_chunk, requested_model)


async def _metered_astream(
    adapter: MeterAdapter,
    verdict: Verdict,
    stream: Any,
    *,
    requested_model: object,
) -> Any:
    """Pass an async streamed response through, booking once it ends (T5-6).

    The async twin of `_metered_stream`, and it books on every exit path for the
    same reason: the vendor generated — and charged for — whatever was streamed
    before the exit, so an abandoned stream is booked, never voided.
    """
    last_usage_chunk: object | None = None
    try:
        async for chunk in stream:
            if _usage_bearing(adapter, chunk):
                last_usage_chunk = chunk
            yield chunk
    except BaseException:
        await _book_streamed_async(
            adapter, verdict, last_usage_chunk, requested_model
        )
        raise
    await _book_streamed_async(adapter, verdict, last_usage_chunk, requested_model)


def _book_streamed(
    adapter: MeterAdapter,
    verdict: Verdict,
    usage_chunk: object | None,
    requested_model: object,
) -> None:
    """Book a finished stream from its usage chunk, or from the estimate."""
    if usage_chunk is None:
        # No chunk carried usage — OpenAI omits it unless the caller passes
        # `stream_options={"include_usage": True}`. Book the authorized estimate
        # rather than lose the spend, and say so.
        _logger.warning(
            "lucnt: streamed %s call carried no token usage "
            "(authorization_id=%s); booking the authorized estimate. Pass "
            'stream_options={"include_usage": True} to settle the real cost.',
            adapter.vendor,
            verdict.authorization_id,
        )
        try:
            capture(verdict, estimated=True)
        except Exception:
            _logger.exception(
                "lucnt: streamed %s spend is UNBOOKED (authorization_id=%s).",
                adapter.vendor,
                verdict.authorization_id,
            )
        return
    _book(adapter, verdict, usage_chunk, requested_model=requested_model)


async def _book_streamed_async(
    adapter: MeterAdapter,
    verdict: Verdict,
    usage_chunk: object | None,
    requested_model: object,
) -> None:
    """Book a finished async stream from its usage chunk, or from the estimate."""
    if usage_chunk is None:
        _logger.warning(
            "lucnt: streamed %s call carried no token usage "
            "(authorization_id=%s); booking the authorized estimate. Pass "
            'stream_options={"include_usage": True} to settle the real cost.',
            adapter.vendor,
            verdict.authorization_id,
        )
        try:
            await capture_async(verdict, estimated=True)
        except Exception:
            _logger.exception(
                "lucnt: streamed %s spend is UNBOOKED (authorization_id=%s).",
                adapter.vendor,
                verdict.authorization_id,
            )
        return
    await _book_async(
        adapter, verdict, usage_chunk, requested_model=requested_model
    )


class _MeteredStreamManager:
    """Proxy for a vendor streaming context manager, metered on exit (T5-5).

    Anthropic's `client.messages.stream(...)` returns a context manager, not an
    iterator, so it needs its own wrapper: authorize on entry, then read the
    completed message on the way out via the documented
    `stream.get_final_message()` helper.

    Authorizes lazily in `__enter__` (F-11), matching the async twin: a manager
    that is constructed and never entered mints no hold, and the vendor's own
    stream is not opened either. The failure ordering inside `__enter__` is the
    gate's: authorize first (raising there is policy working, nothing moved),
    and a vendor failure *after* the hold releases it before re-raising.

    Metering is best-effort, per T5-1: if the vendor's stream object does not
    expose what we expect, the caller's stream still works and the authorized
    estimate is booked instead.
    """

    def __init__(
        self,
        make_manager: Callable[[], Any],
        adapter: MeterAdapter,
        request_kwargs: Mapping[str, Any],
        *,
        agent: str | None,
        entity: str | None,
        intent: str | None,
        project: str | None,
    ) -> None:
        """Store everything needed to authorize and enter on `__enter__`."""
        self._make_manager = make_manager
        self._adapter = adapter
        self._request_kwargs = request_kwargs
        self._requested_model = request_kwargs.get("model")
        self._agent = agent
        self._entity = entity
        self._intent = intent
        self._project = project
        self._inner: Any = None
        self._stream: Any = None
        self._verdict: Verdict | None = None

    def __enter__(self) -> Any:
        """Authorize, enter the vendor's stream, and return its stream object."""
        adapter = self._adapter
        model = self._requested_model
        self._verdict = authorize(
            vendor=adapter.vendor,
            amount=adapter.estimate_cost(self._request_kwargs),
            currency=adapter.currency,
            reason=f"{adapter.vendor} completion ({model}, streamed)",
            agent=self._agent,
            entity=self._entity,
            intent=self._intent,
            project=self._project,
            model=model if isinstance(model, str) else None,
            strict=True,
        )
        try:
            with wrapper_governed():
                self._inner = self._make_manager()
                self._stream = self._inner.__enter__()
        except BaseException as exc:
            # Nothing streamed, so no money moved — release the hold and let the
            # vendor's own error through untouched.
            _release(
                self._verdict, f"{type(exc).__name__} entering {adapter.vendor} stream"
            )
            raise
        return self._stream

    def __exit__(self, *exc_info: Any) -> Any:
        """Leave the vendor's stream, then book the completed message."""
        result = self._inner.__exit__(*exc_info)
        final = None
        try:
            final = self._stream.get_final_message()
        except Exception:
            _logger.warning(
                "lucnt: could not read the final message of a streamed %s call "
                "(authorization_id=%s); booking the authorized estimate.",
                self._adapter.vendor,
                self._verdict.authorization_id if self._verdict else None,
                exc_info=True,
            )
        if self._verdict is not None:
            _book_streamed(
                self._adapter, self._verdict, final, self._requested_model
            )
        return result

    def __getattr__(self, name: str) -> Any:
        """Delegate anything else to the wrapped manager."""
        return getattr(self._inner, name)


class _MeteredAsyncStreamManager:
    """Proxy for an async vendor streaming context manager, metered on exit (T5-6).

    Holds off on authorizing until `__aenter__`. That is forced rather than
    chosen: the wrapper this replaces is called *synchronously* by `async with`,
    so there is no earlier await point.

    It also happens to be the better shape — a manager that is constructed and
    never entered mints no hold at all — which is why the sync
    `_MeteredStreamManager` was reshaped to match it (F-11).

    Metering stays best-effort per T5-1: if the vendor's stream object does not
    expose what we expect, the caller's stream still works and the authorized
    estimate is booked instead.
    """

    def __init__(
        self,
        make_manager: Callable[[], Any],
        adapter: MeterAdapter,
        request_kwargs: Mapping[str, Any],
        *,
        agent: str | None,
        entity: str | None,
        intent: str | None,
        project: str | None,
    ) -> None:
        """Store everything needed to authorize and enter on `__aenter__`."""
        self._make_manager = make_manager
        self._adapter = adapter
        self._request_kwargs = request_kwargs
        self._requested_model = request_kwargs.get("model")
        self._agent = agent
        self._entity = entity
        self._intent = intent
        self._project = project
        self._inner: Any = None
        self._stream: Any = None
        self._verdict: Verdict | None = None

    async def __aenter__(self) -> Any:
        """Authorize, enter the vendor's stream, and return its stream object."""
        adapter = self._adapter
        model = self._requested_model
        self._verdict = await authorize_async(
            vendor=adapter.vendor,
            amount=adapter.estimate_cost(self._request_kwargs),
            currency=adapter.currency,
            reason=f"{adapter.vendor} completion ({model}, streamed)",
            agent=self._agent,
            entity=self._entity,
            intent=self._intent,
            project=self._project,
            model=model if isinstance(model, str) else None,
            strict=True,
        )
        try:
            with wrapper_governed():
                self._inner = self._make_manager()
                self._stream = await self._inner.__aenter__()
        except BaseException as exc:
            # Nothing streamed, so no money moved — release the hold and let the
            # vendor's own error through untouched.
            await _release_async(
                self._verdict, f"{type(exc).__name__} entering {adapter.vendor} stream"
            )
            raise
        return self._stream

    async def __aexit__(self, *exc_info: Any) -> Any:
        """Leave the vendor's stream, then book the completed message."""
        result = await self._inner.__aexit__(*exc_info)
        final = None
        try:
            final = await self._stream.get_final_message()
        except Exception:
            _logger.warning(
                "lucnt: could not read the final message of a streamed %s call "
                "(authorization_id=%s); booking the authorized estimate.",
                self._adapter.vendor,
                self._verdict.authorization_id if self._verdict else None,
                exc_info=True,
            )
        if self._verdict is not None:
            await _book_streamed_async(
                self._adapter, self._verdict, final, self._requested_model
            )
        return result

    def __getattr__(self, name: str) -> Any:
        """Delegate anything else to the wrapped manager."""
        return getattr(self._inner, name)


def _release(verdict: Verdict, reason: str) -> None:
    """Void a hold whose vendor call failed, never raising (T5-4).

    Called from inside an `except` block, so it must not raise: replacing the
    vendor's own error with a lucnt error would hide the only thing the caller
    actually needs to see. A hold that cannot be released still expires via the
    server-side TTL, so the worst case is a bounded stale reservation rather than
    a permanent one.
    """
    try:
        void(verdict, reason=reason)
    except Exception:
        _logger.warning(
            "lucnt: could not release the hold for a failed %s call "
            "(authorization_id=%s); it will stop reserving budget when the "
            "server-side TTL expires it.",
            reason,
            verdict.authorization_id,
            exc_info=True,
        )


async def _release_async(verdict: Verdict, reason: str) -> None:
    """Void a hold whose async vendor call failed, never raising (T5-4).

    Called from inside an `except` block, so it must not raise: replacing the
    vendor's own error with a lucnt error would hide the only thing the caller
    actually needs to see.
    """
    try:
        await void_async(verdict, reason=reason)
    except Exception:
        _logger.warning(
            "lucnt: could not release the hold for a failed %s call "
            "(authorization_id=%s); it will stop reserving budget when the "
            "server-side TTL expires it.",
            reason,
            verdict.authorization_id,
            exc_info=True,
        )


async def _book_async(
    adapter: MeterAdapter,
    verdict: Verdict,
    response: object,
    *,
    requested_model: object,
) -> None:
    """Capture the real cost, never raising — the response is already paid for.

    The async twin of `_book`, degrading in the same three steps: settle with
    actual usage, else capture the hold as authorized, else log the orphaned
    hold with its authorization id. `Exception` and not `BaseException` so a
    cancellation still propagates.
    """
    try:
        usage = adapter.usage_from_response(response)
        await capture_async(
            verdict,
            amount=adapter.cost(
                usage,
                fallback_model=requested_model
                if isinstance(requested_model, str)
                else None,
            ),
            model=usage.model,
            tokens=usage.input_tokens + usage.output_tokens,
        )
        return
    except Exception:
        _logger.exception(
            "lucnt: could not settle metered %s spend with actual usage "
            "(authorization_id=%s, requested_model=%r); the vendor response was "
            "returned to the caller unharmed. Falling back to booking the "
            "authorized estimate.",
            adapter.vendor,
            verdict.authorization_id,
            requested_model,
        )
    try:
        await capture_async(verdict, estimated=True)
    except Exception:
        _logger.exception(
            "lucnt: metered %s spend is UNBOOKED and its hold is orphaned "
            "(authorization_id=%s, requested_model=%r). The vendor was paid and "
            "the response was returned; this transaction needs reconciliation.",
            adapter.vendor,
            verdict.authorization_id,
            requested_model,
        )


def _book(
    adapter: MeterAdapter,
    verdict: Verdict,
    response: object,
    *,
    requested_model: object,
) -> None:
    """Capture the real cost, never raising — the response is already paid for.

    `meter()`'s failure mode before T5-1 was the expensive one: reading usage,
    pricing it, or reaching the backend could raise *after* `original()` had
    returned, so the customer lost a completion they had just paid the vendor
    for. Metering is worth strictly less than the thing being metered, so this
    swallows everything and degrades in two steps instead:

    1. Settle with the actual cost, model, and token counts (the normal path).
    2. If reading or pricing usage failed, capture the hold **as authorized**,
       flagged `estimated=True` (F-8). The estimate is then what lands in the
       ledger — worse than the truth, but far better than losing the spend,
       since attribution at the moment of spend is the product (M1) — and the
       flag makes those rows queryable rather than an ERROR-log archaeology dig.
    3. If the backend itself is unreachable, the hold is orphaned. Logged with
       the authorization id so it can be reconciled (M7) or released (T5-4).

    `Exception` and not `BaseException`: a `KeyboardInterrupt` or `SystemExit`
    must still propagate. Nothing here is silenced — every branch logs.
    """
    try:
        usage = adapter.usage_from_response(response)
        capture(
            verdict,
            amount=adapter.cost(
                usage,
                fallback_model=requested_model
                if isinstance(requested_model, str)
                else None,
            ),
            model=usage.model,
            tokens=usage.input_tokens + usage.output_tokens,
        )
        return
    except Exception:
        _logger.exception(
            "lucnt: could not settle metered %s spend with actual usage "
            "(authorization_id=%s, requested_model=%r); the vendor response was "
            "returned to the caller unharmed. Falling back to booking the "
            "authorized estimate.",
            adapter.vendor,
            verdict.authorization_id,
            requested_model,
        )
    try:
        # No amount override: the authorization's held estimate stands, and the
        # row is flagged as an estimate so the ledger never presents it as
        # settled truth (F-8).
        capture(verdict, estimated=True)
    except Exception:
        _logger.exception(
            "lucnt: metered %s spend is UNBOOKED and its hold is orphaned "
            "(authorization_id=%s, requested_model=%r). The vendor was paid and "
            "the response was returned; this transaction needs reconciliation.",
            adapter.vendor,
            verdict.authorization_id,
            requested_model,
        )


def meter(
    client: ClientT,
    *,
    agent: str | None = None,
    entity: str | None = None,
    intent: str | None = None,
    project: str | None = None,
    pricing: Mapping[str, tuple[PriceInput, PriceInput]] | None = None,
    unmetered: Unmetered = "block",
) -> ClientT:
    """Return a governed proxy of `client` that authorizes+captures each call.

    `agent`/`entity`/`intent`/`project` fall back to the ambient `task()`
    context (they are forwarded to `authorize()` unchanged).

    Works on both client flavours — `OpenAI()` and `AsyncOpenAI()`,
    `Anthropic()` and `AsyncAnthropic()` (T5-6). The returned proxy matches
    whichever it was given, so an async client's metered methods stay awaitable
    and its streams stay `async for` / `async with`.

    **Every call path is governed, not just the documented methods** (F-1).
    Besides wrapping the completion methods, `meter()` installs a governor on
    the client's underlying HTTP transport, so `with_raw_response`,
    `with_streaming_response`, `responses`, `beta.*`, and `embeddings` calls
    are authorized and captured too. The install is **in place**: even code
    holding a reference to the original, un-proxied client is governed. Two
    consequences worth knowing:

    * On the wrapped methods a policy block raises `PolicyBlocked` /
      `BudgetExhausted` as always. On every *other* path a block surfaces as
      the vendor SDK's own 403 error (e.g. `openai.PermissionDeniedError`)
      carrying the lucnt reason — raising inside the vendor's HTTP layer would
      be wrapped into a connection error and retried, so a synthetic refusal
      is the loud, unretried option.
    * A POST to a vendor path lucnt cannot price (audio, images, batches,
      fine-tuning) is refused fail-closed by default; see `unmetered`.

    **Turn off the vendor client's own retries.** A vendor SDK that retries
    internally does so *inside* the wrapped call, so lucnt sees one authorize and
    one capture while the vendor may have served — and charged for — more than
    one request. Construct it with `max_retries=0` and retry at the lucnt layer
    instead, wrapping the call in `lucnt.operation()` so the retry replays the
    original hold rather than minting a second one (T5-10).

    Args:
        client: The vendor LLM client to wrap (OpenAI or Anthropic, sync or async).
        agent: Agent handle to attribute metered spend to. Falls back to `task()`.
        entity: Legal entity handle. Falls back to `task()`.
        intent: Free-text purpose of the task. Falls back to `task()`.
        project: Cost-center / project label. Falls back to `task()`.
        pricing: Per-model `(input, output)` price per 1M tokens, merged over
            the packaged pricing pack — the escape hatch for a model the pack
            doesn't know yet, or a rate you negotiated. Pass money as `Decimal`,
            `int`, or `str`; `float` is rejected because it cannot represent a
            price exactly. Example:
            `meter(client, pricing={"my-model": ("3.00", "15.00")})`.
        unmetered: What the transport governor does with a spend-shaped POST it
            cannot price: `"block"` (default) refuses it with a synthetic 403
            before any money moves; `"allow"` forwards it ungoverned, logged
            once per path.

    Returns:
        A proxy typed as the same client — call it exactly as you would the
        original. Each completion call authorizes an estimate, then captures
        the real cost/model/tokens from the response.

    Raises:
        LucntError: The client type has no adapter, a `pricing` entry is
            malformed, or the client exposes no httpx client to govern —
            failing loud rather than leaving unwrapped call paths silently
            unmetered (F-1).
        PolicyBlocked / BudgetExhausted: A metered call was blocked by policy.
    """
    adapter = resolve_adapter(client, pricing=pricing)
    if adapter is None:
        raise LucntError(
            f"meter() has no adapter for client "
            f"{type(client).__module__}.{type(client).__qualname__} "
            f"(supported: anthropic, openai — sync and async)"
        )
    # F-1: the safety net under the wrappers below. Installed first so a client
    # we cannot govern fails loud before any wrapping half-succeeds.
    install_governor(
        client,
        adapter,
        agent=agent,
        entity=entity,
        intent=intent,
        project=project,
        unmetered=unmetered,
    )
    # One adapter serves both client flavours; only the wrapper differs (T5-6).
    if adapter.is_async(client):
        make_call = _make_async_metered_call
        make_stream_call = _make_async_metered_stream_call
    else:
        make_call = _make_metered_call
        make_stream_call = _make_metered_stream_call

    original = client
    for name in adapter.method_path:
        original = getattr(original, name)
    overrides: dict[tuple[str, ...], object] = {
        adapter.method_path: make_call(
            adapter,
            cast("Callable[..., Any]", original),
            agent=agent,
            entity=entity,
            intent=intent,
            project=project,
        )
    }
    # T5-5: a vendor with a separate streaming helper needs it wrapped too, or
    # every streamed call slips past unmetered.
    if adapter.stream_method_path is not None:
        stream_original: object = client
        for name in adapter.stream_method_path:
            stream_original = getattr(stream_original, name, None)
            if stream_original is None:
                break
        if stream_original is not None:
            overrides[adapter.stream_method_path] = make_stream_call(
                adapter,
                cast("Callable[..., Any]", stream_original),
                agent=agent,
                entity=entity,
                intent=intent,
                project=project,
            )
    return cast("ClientT", _patch_paths(client, overrides))
