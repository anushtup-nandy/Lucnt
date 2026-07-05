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
so there is no falsy-return option. Only synchronous clients are supported in
this MVP; an async client raises a clear error.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar, cast

from lucnt.adapters import MeterAdapter, resolve_adapter
from lucnt.exceptions import LucntError
from lucnt.ledger import authorize, capture

ClientT = TypeVar("ClientT")


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


def _patch_path(root: object, path: list[str], replacement: object) -> _Patched:
    """Proxy `root` so the attribute chain `path` resolves to `replacement`."""
    head, *rest = path
    if not rest:
        return _Patched(root, {head: replacement})
    child = _patch_path(getattr(root, head), rest, replacement)
    return _Patched(root, {head: child})


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
        # Authorize a pre-call estimate; strict=True raises if policy blocks,
        # so the real call below never runs on a blocked spend.
        verdict = authorize(
            vendor=adapter.vendor,
            amount=adapter.estimate_cost(kwargs),
            currency="USD",
            reason=f"{adapter.vendor} completion ({model})",
            agent=agent,
            entity=entity,
            intent=intent,
            project=project,
            model=model if isinstance(model, str) else None,
            strict=True,
        )
        response = original(*args, **kwargs)
        usage = adapter.usage_from_response(response)
        capture(
            verdict,
            amount=adapter.cost(usage),
            model=usage.model,
            tokens=usage.input_tokens + usage.output_tokens,
        )
        return response

    return metered_call


def meter(
    client: ClientT,
    *,
    agent: str | None = None,
    entity: str | None = None,
    intent: str | None = None,
    project: str | None = None,
) -> ClientT:
    """Return a governed proxy of `client` that authorizes+captures each call.

    `agent`/`entity`/`intent`/`project` fall back to the ambient `task()`
    context (they are forwarded to `authorize()` unchanged).

    Returns:
        A proxy typed as the same client — call it exactly as you would the
        original. Each completion call authorizes an estimate, then captures
        the real cost/model/tokens from the response.

    Raises:
        LucntError: The client type has no adapter (or is an async client).
        PolicyBlocked / BudgetExhausted: A metered call was blocked by policy.
    """
    adapter = resolve_adapter(client)
    if adapter is None:
        raise LucntError(
            f"meter() has no adapter for client "
            f"{type(client).__module__}.{type(client).__qualname__} "
            f"(supported: anthropic, openai; sync clients only)"
        )
    original = client
    for name in adapter.method_path:
        original = getattr(original, name)
    metered = _make_metered_call(
        adapter,
        cast("Callable[..., Any]", original),
        agent=agent,
        entity=entity,
        intent=intent,
        project=project,
    )
    proxy = _patch_path(client, list(adapter.method_path), metered)
    return cast("ClientT", proxy)
