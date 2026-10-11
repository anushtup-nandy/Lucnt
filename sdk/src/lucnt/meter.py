"""meter(client): auto-instrument an LLM client's spend via authorize/capture.

Raising before the vendor call is the policy gate; raising after it is never
allowed (the customer has paid), so post-call bookkeeping is best-effort.
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

_logger = logging.getLogger("lucnt.meter")


class _Patched:
    """Transparent proxy that delegates everything except `overrides`."""

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

    All paths at once: sibling overrides (`messages.create`/`.stream`) share a
    parent proxy.
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
        verdict = authorize(
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
                response = original(*args, **kwargs)
        except BaseException as exc:
            # No money moved: release the hold, re-raise the vendor's error as-is.
            _release(verdict, f"{type(exc).__name__} from {adapter.vendor} call")
            raise
        # The vendor has been paid: bookkeeping below must never raise.
        if streaming:
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
    """Wrap a vendor streaming helper; the hold is taken lazily in `__enter__`."""

    def metered_stream_call(*args: Any, **kwargs: Any) -> Any:
        return _MeteredStreamManager(
            lambda: original(*args, **kwargs),
            adapter,
            # Whole request: the estimate is sized from the prompt.
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
    """Async `_make_metered_call`."""

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
        if streaming:
            return _metered_astream(adapter, verdict, response, requested_model=model)
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
    """Wrap an async vendor streaming helper.

    Deliberately not `async def`: `async with` needs `__aenter__` on the result.
    """

    def metered_stream_call(*args: Any, **kwargs: Any) -> Any:
        return _MeteredAsyncStreamManager(
            lambda: original(*args, **kwargs),
            adapter,
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
    """Pass a streamed response through, booking once on any exit path.

    An abandoned stream is booked, never voided: its tokens were charged.
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
    """Async `_metered_stream`."""
    last_usage_chunk: object | None = None
    try:
        async for chunk in stream:
            if _usage_bearing(adapter, chunk):
                last_usage_chunk = chunk
            yield chunk
    except BaseException:
        await _book_streamed_async(adapter, verdict, last_usage_chunk, requested_model)
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
    await _book_async(adapter, verdict, usage_chunk, requested_model=requested_model)


class _MeteredStreamManager:
    """Proxy for a vendor streaming context manager, metered on exit.

    Authorizes lazily in `__enter__`, so a never-entered manager holds nothing.
    Books from `get_final_message()`, else the estimate.
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
            _book_streamed(self._adapter, self._verdict, final, self._requested_model)
        return result

    def __getattr__(self, name: str) -> Any:
        """Delegate anything else to the wrapped manager."""
        return getattr(self._inner, name)


class _MeteredAsyncStreamManager:
    """Async `_MeteredStreamManager`; authorizes in `__aenter__`."""

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
    """Void a hold whose vendor call failed, never raising (would mask its error)."""
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
    """Async `_release`."""
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
    """Async `_book`."""
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
    """Capture the real cost, never raising: the response is already paid for.

    Degrades: actual usage, else the hold flagged `estimated=True`, else log the
    orphaned hold. Catches `Exception` only, so interrupts still propagate.
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

        client = lucnt.meter(OpenAI(max_retries=0), agent="research", entity="in")
        resp = client.chat.completions.create(model="gpt-4o", messages=[...])

    Supports sync and async OpenAI/Anthropic clients. Also governs the client's
    HTTP transport in place, so every call path is metered; off the wrapped
    methods a block surfaces as the vendor's 403 error. Set the vendor client's
    `max_retries=0`: its internal retries are invisible to lucnt.

    Args:
        client: The OpenAI or Anthropic client (sync or async) to wrap.
        agent: Agent handle; falls back to `task()`.
        entity: Legal entity handle; falls back to `task()`.
        intent: Free-text purpose; falls back to `task()`.
        project: Cost-center / project label; falls back to `task()`.
        pricing: Model id -> `(input, output)` per 1M tokens; wins over the pack.
        unmetered: `"block"` refuses unpriceable POST paths; `"allow"` forwards.

    Returns:
        A proxy typed as the same client.

    Raises:
        LucntError: No adapter, malformed `pricing`, or no httpx client to govern.
        PolicyBlocked: A metered call was blocked by policy.
        BudgetExhausted: A metered call was blocked by a budget cap.
    """
    adapter = resolve_adapter(client, pricing=pricing)
    if adapter is None:
        raise LucntError(
            f"meter() has no adapter for client "
            f"{type(client).__module__}.{type(client).__qualname__} "
            f"(supported: anthropic, openai — sync and async)"
        )
    # First, so an ungovernable client fails before wrapping half-succeeds.
    install_governor(
        client,
        adapter,
        agent=agent,
        entity=entity,
        intent=intent,
        project=project,
        unmetered=unmetered,
    )
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
