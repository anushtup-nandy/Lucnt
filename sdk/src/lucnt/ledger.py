"""authorize() / capture() / spend() — the universal spend-type-agnostic primitive.

Every agent-initiated spend flows through this pair: `authorize()` lets
policy block *before* money moves; `capture()` commits the real spend to
the ledger. `spend()` is the atomic convenience for callers with nothing to
gate mid-way. Spend-type-agnostic: a flight, a SaaS seat, and a token batch
all flow through the same primitive (`model`/`tokens` are optional, for the
API/LLM subset only).

**Every function has an `_async` twin** (T5-6). The pair shares one
`_Call` — the method, path, body, idempotency key, and retry-safety of the
request — so the two paths cannot disagree about the wire contract. That is the
point of the indirection: an async variant that forgot to send the idempotency
key, or posted to a different path, would be a silent double-book that only
async callers hit. Building the request once makes that class of drift
unrepresentable rather than merely unlikely.

Calling a synchronous function from inside a running event loop raises rather
than blocking it: a 30-second policy call that stalls every other coroutine in
the process surfaces as unrelated timeouts elsewhere, which is far harder to
diagnose than an error naming the async variant.
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import threading
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Literal, TypeVar
from weakref import WeakKeyDictionary

import httpx
from pydantic import BaseModel, ValidationError

from lucnt.client import (
    AsyncTransport,
    Transport,
    check_idempotency_key,
    truncate_body,
)
from lucnt.config import get_config
from lucnt.context import get_current_operation, get_current_task
from lucnt.exceptions import (
    BudgetExhausted,
    ConfigurationError,
    ContractError,
    LucntError,
    PolicyBlocked,
)
from lucnt.models import (
    BudgetStatus,
    FinancialMemory,
    LucntConfig,
    Money,
    ReconException,
    SpendRequest,
    Transaction,
    Verdict,
    VoidResult,
)

_BUDGET_EXHAUSTED_CODE = "budget_exhausted"

_ModelT = TypeVar("_ModelT", bound=BaseModel)

_transport_lock = threading.Lock()
_transport: Transport | None = None
_transport_config: LucntConfig | None = None
# Pools displaced by a genuine reconfigure. Never closed inline — see below.
_retired_transports: list[Transport] = []

_async_lock = threading.Lock()
# Keyed by event loop, not process-wide (T5-6). An httpx.AsyncClient's pool holds
# loop-bound objects, so a pool built under one `asyncio.run()` cannot serve
# requests under the next one — a process-wide singleton would hand out a pool
# bound to a closed loop. Weak keys so a finished loop's entry disappears with
# it rather than pinning the loop object alive for the process's lifetime.
_async_transports: WeakKeyDictionary[
    asyncio.AbstractEventLoop, tuple[AsyncTransport, LucntConfig]
] = WeakKeyDictionary()


def _get_transport() -> Transport:
    """Return the process-wide pooled Transport, rebuilding it if reconfigured.

    Thread-safe on three counts (T5-8):

    1. The check-and-build is under a lock. Unsynchronized, two threads racing
       the first call each build a pool and one is silently leaked.
    2. Configs are compared **by value**, not identity. `configure()` returns a
       fresh object every call, so an identity check rebuilt the pool on any
       re-`configure()` — even one passing identical settings.
    3. A displaced pool is **retired, not closed**. Closing inline would pull
       the socket out from under a request in flight on another thread; httpx
       raises a bare `RuntimeError` for that, which is outside the SDK's
       exception hierarchy and — now that retries exist — is not something the
       transport would recognise as retryable. Retired pools are closed at
       interpreter exit instead. Reconfiguring is a startup-time operation, so
       the bounded cost is a handful of idle pools in the rare process that
       does it repeatedly; the alternative is a torn request in a money path.
    """
    global _transport, _transport_config
    config = get_config()
    with _transport_lock:
        if _transport is not None and _transport_config == config:
            return _transport
        if _transport is not None:
            _retired_transports.append(_transport)
        _transport = Transport(config)
        _transport_config = config
        return _transport


def _require_running_loop() -> asyncio.AbstractEventLoop:
    """Return the running loop, or explain that the async API needs one."""
    try:
        return asyncio.get_running_loop()
    except RuntimeError as exc:
        raise ConfigurationError(
            "lucnt's async functions must be awaited inside a running event "
            "loop. Drive them with asyncio.run(...), or call the synchronous "
            "functions (lucnt.spend, lucnt.authorize, ...) instead."
        ) from exc


def _forbid_running_loop(sync_name: str, async_name: str) -> None:
    """Refuse a blocking call made from inside an event loop (T5-6).

    The failure this prevents is not the caller's own latency — it is that a
    synchronous request blocks the *whole loop* for up to the request timeout,
    so every unrelated coroutine in the process stalls with it. That surfaces as
    mysterious timeouts in other subsystems, which is why this raises instead of
    warning: an explicit error naming the async variant costs one line to fix,
    a stalled loop costs an afternoon to diagnose.

    Running the sync function in a worker thread stays valid — there is no
    running loop *in that thread*, so this does not fire on
    `asyncio.to_thread(lucnt.spend, ...)`.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise ConfigurationError(
        f"lucnt.{sync_name}() is synchronous and was called from a running "
        f"event loop, where it would block every other coroutine in the process "
        f"for up to the request timeout. Await lucnt.{async_name}() instead, or "
        f"run it off the loop with asyncio.to_thread(lucnt.{sync_name}, ...)."
    )


def _get_async_transport() -> AsyncTransport:
    """Return the pooled AsyncTransport for the running loop, rebuilding on reconfigure.

    Mirrors `_get_transport`'s locking and by-value config comparison. A pool
    displaced by a genuine reconfigure is simply dropped rather than retired:
    closing it needs an `await`, and this function is deliberately synchronous
    (building an `httpx.AsyncClient` does no I/O) so every async entry point can
    call it without an extra round trip. Reconfiguring is a startup-time
    operation, so the dropped pool is collected with nothing in flight.
    """
    loop = _require_running_loop()
    config = get_config()
    with _async_lock:
        existing = _async_transports.get(loop)
        if existing is not None and existing[1] == config:
            return existing[0]
        transport = AsyncTransport(config)
        _async_transports[loop] = (transport, config)
        return transport


async def aclose() -> None:
    """Close the async connection pool belonging to the running event loop.

    Await this before the loop shuts down if you want sockets released
    deterministically:

        async def main() -> None:
            try:
                await lucnt.spend_async(...)
            finally:
                await lucnt.aclose()

    Optional, and a no-op when this loop never made an async call. Skipping it
    leaks nothing beyond the process: the pool is garbage-collected with the
    loop and the OS reclaims its sockets at exit. Async pools are deliberately
    *not* closed by the interpreter-exit handler that closes the synchronous
    ones — that runs with no loop available, and driving a fresh loop to close a
    client bound to a dead one is a shutdown-time traceback rather than a
    cleanup.
    """
    loop = _require_running_loop()
    with _async_lock:
        entry = _async_transports.pop(loop, None)
    if entry is not None:
        await entry[0].aclose()


@atexit.register
def _close_transports() -> None:
    """Close the live sync pool and every retired one at interpreter exit."""
    with _transport_lock:
        pools = [*_retired_transports, *([_transport] if _transport else [])]
        _retired_transports.clear()
    for pool in pools:
        # Never raise during interpreter exit — a failed close here would
        # surface as a confusing shutdown traceback and hide the real work.
        with contextlib.suppress(Exception):
            pool.close()


@dataclass(frozen=True)
class _Call:
    """One HTTP call, fully described but not yet made.

    The sync and async variants of each operation build the *same* `_Call`, so
    the path, body, idempotency key, and retry-safety of a request are decided
    in exactly one place. A drift between the two on any of those is a
    double-booked spend rather than a cosmetic inconsistency (T5-6).
    """

    method: str
    path: str
    json: dict[str, object] | None = None
    params: dict[str, str] | None = None
    idempotency_key: str | None = None
    retry_safe: bool = False


def _payload(response: httpx.Response, path: str) -> dict[str, object]:
    """Read a success body as a JSON object, or raise `ContractError` (T5-10).

    Every endpoint the SDK calls answers with a JSON object. A body that is not
    one means this SDK version and the backend disagree — which used to escape
    as a bare `json.JSONDecodeError`, outside the SDK's hierarchy, so a caller's
    `except LucntError` around a money path did not cover it.
    """
    try:
        parsed = response.json()
    except ValueError as exc:
        raise ContractError(
            f"{path} returned {response.status_code} with a body that is not "
            f"JSON: {truncate_body(response.text)!r}",
            path=path,
        ) from exc
    if not isinstance(parsed, dict):
        raise ContractError(
            f"{path} returned a JSON {type(parsed).__name__}, not an object.",
            path=path,
        )
    return parsed


def _model(model: type[_ModelT], payload: dict[str, object], path: str) -> _ModelT:
    """Validate a response body into `model`, or raise `ContractError` (T5-10).

    A `pydantic.ValidationError` escaping from here would be both outside the
    SDK's hierarchy and misleading — it reads like the *caller* sent something
    invalid, when in fact the server's response is the thing that did not match.
    """
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        raise ContractError(
            f"{path} returned a body this lucnt version cannot read as "
            f"{model.__name__} — the backend may be newer than the SDK. {exc}",
            path=path,
        ) from exc


def _send(call: _Call) -> dict[str, object]:
    """Make `call` synchronously and return its parsed body."""
    response = _get_transport().request(
        call.method,
        call.path,
        json=call.json,
        params=call.params,
        idempotency_key=call.idempotency_key,
        retry_safe=call.retry_safe,
    )
    return _payload(response, call.path)


async def _send_async(call: _Call) -> dict[str, object]:
    """Make `call` on the running loop and return its parsed body."""
    response = await _get_async_transport().request(
        call.method,
        call.path,
        json=call.json,
        params=call.params,
        idempotency_key=call.idempotency_key,
        retry_safe=call.retry_safe,
    )
    return _payload(response, call.path)


def _resolve_required(explicit: str | None, ambient: str | None, field: str) -> str:
    """Prefer an explicit arg over ambient task() context; error if neither is set."""
    value = explicit if explicit is not None else ambient
    if value is None:
        raise ConfigurationError(
            f"'{field}' is required: pass it explicitly or set it via lucnt.task()."
        )
    return value


def _as_decimal(amount: Money, field: str = "amount") -> Decimal:
    """Normalize an exact money input to `Decimal`, rejecting `float` (T5-10).

    Converts via the string form so a plain `amount=10` stays exact.

    `float` is refused rather than repaired. `Decimal(1.83)` is not
    `Decimal("1.83")`, and quietly routing floats through `str()` taught callers
    that float money is fine — right up until one of them computed an amount
    (`0.1 + 0.2`) and booked `0.30000000000000004`. A ledger has to be exact at
    its boundary, so the boundary says so.

    Args:
        amount: `Decimal`, `int`, or `str`.
        field: Parameter name, named in the error message.

    Returns:
        The amount as an exact `Decimal`.

    Raises:
        ConfigurationError: The value is a `float`, a `bool`, another
            unsupported type, or a string that is not a number.
    """
    if isinstance(amount, Decimal):
        return amount
    # bool before int: `amount=True` would otherwise book 1.00 of the currency.
    if isinstance(amount, bool) or not isinstance(amount, (int, str)):
        raise ConfigurationError(
            f"{field} must be Decimal, int, or str — got "
            f"{type(amount).__name__}. float is rejected because it cannot "
            f"represent money exactly (Decimal(1.83) is not Decimal('1.83')); "
            f"pass Decimal('1.83') or '1.83'."
        )
    try:
        return Decimal(amount)
    except InvalidOperation as exc:
        raise ConfigurationError(f"{field} is not a number: {amount!r}") from exc


def _new_idempotency_key() -> str:
    """Mint a key for one logical authorize, reused across its transport retries.

    Auto-generating per call covers the transport-retry case (T5-3): the same
    key goes out on every attempt, so a request that reached the server before
    the connection dropped replays instead of minting a second hold. It does
    *not* cover an application-level retry — calling `authorize()` again is a
    new logical spend and gets a new key. `lucnt.operation()` (T5-11) is the
    mechanism for that case; an explicit `idempotency_key=` still overrides both.
    """
    return uuid.uuid4().hex


# Fields excluded from an operation scope's payload guardrail: free-text labels
# and LLM attribution metadata. Everything else is included **by default**, so a
# field added to `SpendRequest` later is fingerprinted unless someone
# deliberately exempts it — the fail-safe direction for a correctness check.
# The excluded fields cannot cause a wrong *approval*: the verdict and the
# booked money follow agent/entity/vendor/amount/currency and the tax fields,
# all of which are compared. Keeping cosmetic drift out of the check is what
# stops it becoming a nuisance customers route around by not using operation().
_FINGERPRINT_EXCLUDED = frozenset({"intent", "model", "project", "reason", "tokens"})


def _operation_fingerprint(request: SpendRequest) -> dict[str, object]:
    """Reduce a request to the fields that decide which spend it is.

    Dumped in python mode, not JSON: it keeps `amount` a `Decimal`, so
    `amount=10` and `amount="10.00"` compare equal instead of tripping the
    guardrail on a difference that isn't one.
    """
    return {
        field: value
        for field, value in request.model_dump().items()
        if field not in _FINGERPRINT_EXCLUDED
    }


def _resolve_idempotency_key(explicit: str | None, request: SpendRequest) -> str:
    """Pick the key for one authorize: explicit → ambient `operation()` → auto.

    An explicit key opts out of the ambient scope entirely, guardrail included —
    the caller has taken over responsibility for the replay semantics.
    """
    if explicit is not None:
        return check_idempotency_key(explicit, "idempotency_key")
    op = get_current_operation()
    if op is None:
        return _new_idempotency_key()
    op.claim(_operation_fingerprint(request))
    return op.op_id


def _raise_for_verdict(verdict: Verdict) -> None:
    """Raise the typed exception matching a non-approved verdict (strict mode)."""
    exc_type = (
        BudgetExhausted if verdict.code == _BUDGET_EXHAUSTED_CODE else PolicyBlocked
    )
    raise exc_type(
        f"spend {verdict.status}: {verdict.reason or verdict.code}",
        status=verdict.status,
        code=verdict.code,
        reason=verdict.reason,
    )


# --------------------------------------------------------------------------- #
# authorize
# --------------------------------------------------------------------------- #

_AUTHORIZE_PATH = "/transactions/authorize"


def _authorize_call(
    *,
    vendor: str,
    amount: Money,
    reason: str,
    currency: str,
    agent: str | None,
    entity: str | None,
    intent: str | None,
    project: str | None,
    category: str | None,
    tax_category: str | None,
    tax_inclusive: bool,
    supply_type: Literal["service", "goods"],
    model: str | None,
    tokens: int | None,
    idempotency_key: str | None,
) -> _Call:
    """Build the authorize request, resolving ambient context and the retry key."""
    ctx = get_current_task()
    request = SpendRequest(
        agent=_resolve_required(agent, ctx.agent if ctx else None, "agent"),
        entity=_resolve_required(entity, ctx.entity if ctx else None, "entity"),
        vendor=vendor,
        amount=_as_decimal(amount),
        currency=currency,
        reason=reason,
        intent=intent if intent is not None else (ctx.intent if ctx else None),
        project=project if project is not None else (ctx.project if ctx else None),
        category=category,
        tax_category=tax_category,
        tax_inclusive=tax_inclusive,
        supply_type=supply_type,
        model=model,
        tokens=tokens,
    )
    return _Call(
        "POST",
        _AUTHORIZE_PATH,
        json=request.model_dump(mode="json"),
        idempotency_key=_resolve_idempotency_key(idempotency_key, request),
    )


def _authorize_result(
    payload: dict[str, object], path: str, *, strict: bool
) -> Verdict:
    """Validate the verdict and, in strict mode, raise on a blocking one."""
    verdict = _model(Verdict, payload, path)
    if strict and not verdict.approved:
        _raise_for_verdict(verdict)
    return verdict


def authorize(
    *,
    vendor: str,
    amount: Money,
    reason: str,
    currency: str,
    agent: str | None = None,
    entity: str | None = None,
    intent: str | None = None,
    project: str | None = None,
    category: str | None = None,
    tax_category: str | None = None,
    tax_inclusive: bool = True,
    supply_type: Literal["service", "goods"] = "service",
    model: str | None = None,
    tokens: int | None = None,
    strict: bool = False,
    idempotency_key: str | None = None,
) -> Verdict:
    """Ask policy for a verdict on a spend, before any money moves.

    `agent`/`entity`/`intent`/`project` fall back to the ambient `task()`
    context when not passed explicitly. With `strict=True`, a non-approved
    verdict raises instead of returning — `BudgetExhausted` if the block was a
    budget cap, else `PolicyBlocked` (which `BudgetExhausted` subclasses).

    Args:
        vendor: The merchant or vendor being paid, e.g. `"United Airlines"`.
        amount: The amount to hold, as `Decimal`, `int`, or `str`. `float` is
            rejected — money must be exact.
        reason: Human-readable justification, stored on the transaction.
        currency: ISO-4217 code of the currency actually being paid in. Never
            defaulted, so a foreign spend is never mislabelled as domestic.
        agent: Agent handle to attribute the spend to. Falls back to `task()`.
        entity: Legal entity handle. Falls back to `task()`.
        intent: Free-text purpose of the task. Falls back to `task()`.
        project: Cost-center / project label. Falls back to `task()`.
        category: Expense category (`software`, `ai_compute`, `travel`,
            `marketing`, `general`). Inferred from the vendor when omitted.
        tax_category: Tax slab override, e.g. `"gst_5"`. Standard rate if omitted.
        tax_inclusive: Whether a domestic `amount` already embeds tax (default)
            or has it added on top.
        supply_type: `"service"` (default) or `"goods"` — a cross-border goods
            spend routes to a customs path rather than the services reverse charge.
        model: Model identifier, for LLM/API spend only.
        tokens: Tokens consumed, for LLM/API spend only.
        strict: Raise on a non-approved verdict instead of returning it.
        idempotency_key: Reuse the same key to make a retry replay the original
            hold instead of minting a second one (T0-6). Resolution order is
            this argument, then an ambient `lucnt.operation()` scope, then a key
            generated for this call alone. The auto key covers the SDK's own
            transport retries; if *your* code retries, wrap the call in
            `lucnt.operation()` or pass a key here.

    Returns:
        A `Verdict`. With `strict=False` (default) a blocking verdict is
        returned, never raised — always check `.approved` before spending.

    Raises:
        PolicyBlocked: `strict=True` and the verdict was not approved.
        BudgetExhausted: `strict=True` and the block was a budget cap.
        IdempotencyConflict: An ambient `operation()` scope is already bound to
            a materially different spend.
        ContractError: The server's response could not be read as a `Verdict`.
        ConfigurationError: `amount` is a `float` or otherwise inexact,
            `idempotency_key` is blank or over 255 characters, `agent`/`entity`
            is resolvable from neither argument nor `task()`, or this was called
            from a running event loop (use `authorize_async`).
    """
    _forbid_running_loop("authorize", "authorize_async")
    call = _authorize_call(
        vendor=vendor,
        amount=amount,
        reason=reason,
        currency=currency,
        agent=agent,
        entity=entity,
        intent=intent,
        project=project,
        category=category,
        tax_category=tax_category,
        tax_inclusive=tax_inclusive,
        supply_type=supply_type,
        model=model,
        tokens=tokens,
        idempotency_key=idempotency_key,
    )
    return _authorize_result(_send(call), call.path, strict=strict)


async def authorize_async(
    *,
    vendor: str,
    amount: Money,
    reason: str,
    currency: str,
    agent: str | None = None,
    entity: str | None = None,
    intent: str | None = None,
    project: str | None = None,
    category: str | None = None,
    tax_category: str | None = None,
    tax_inclusive: bool = True,
    supply_type: Literal["service", "goods"] = "service",
    model: str | None = None,
    tokens: int | None = None,
    strict: bool = False,
    idempotency_key: str | None = None,
) -> Verdict:
    """Await a policy verdict on a spend, before any money moves (T5-6).

    The async twin of `authorize()` — identical semantics, identical wire
    request, identical `task()` / `operation()` context handling (contextvars
    propagate into a coroutine automatically, so `task()` works unchanged around
    an `await`).

    Takes the same arguments as `authorize()`; see there for what each means.

    Returns:
        A `Verdict`. With `strict=False` (default) a blocking verdict is
        returned, never raised — always check `.approved` before spending.

    Raises:
        PolicyBlocked: `strict=True` and the verdict was not approved.
        BudgetExhausted: `strict=True` and the block was a budget cap.
        IdempotencyConflict: An ambient `operation()` scope is already bound to
            a materially different spend.
        ContractError: The server's response could not be read as a `Verdict`.
        ConfigurationError: `amount` is a `float` or otherwise inexact,
            `idempotency_key` is blank or over 255 characters, or
            `agent`/`entity` is resolvable from neither argument nor `task()`.
    """
    call = _authorize_call(
        vendor=vendor,
        amount=amount,
        reason=reason,
        currency=currency,
        agent=agent,
        entity=entity,
        intent=intent,
        project=project,
        category=category,
        tax_category=tax_category,
        tax_inclusive=tax_inclusive,
        supply_type=supply_type,
        model=model,
        tokens=tokens,
        idempotency_key=idempotency_key,
    )
    return _authorize_result(await _send_async(call), call.path, strict=strict)


# --------------------------------------------------------------------------- #
# capture
# --------------------------------------------------------------------------- #

_CAPTURE_PATH = "/transactions/capture"


def _capture_call(
    verdict: Verdict,
    *,
    invoice_url: str | None,
    amount: Money | None,
    model: str | None,
    tokens: int | None,
) -> _Call:
    """Build the capture request, overriding only the values actually settled."""
    body: dict[str, object] = {
        "authorization_id": verdict.authorization_id,
        "source": "sdk",
        "invoice_url": invoice_url,
    }
    if amount is not None:
        body["amount"] = str(_as_decimal(amount))  # Decimal → str for JSON
    if model is not None:
        body["model"] = model
    if tokens is not None:
        body["tokens"] = tokens
    return _Call(
        "POST",
        _CAPTURE_PATH,
        json=body,
        # Safe to retry without a key: the backend dedupes on the
        # authorization_id unique constraint and replays the original
        # transaction, so a repeat can never double-book.
        retry_safe=True,
    )


def capture(
    verdict: Verdict,
    *,
    invoice_url: str | None = None,
    amount: Money | None = None,
    model: str | None = None,
    tokens: int | None = None,
) -> Transaction:
    """Commit the spend approved by `verdict` to the ledger.

    `amount`/`model`/`tokens` **settle** the transaction with values known only
    after the spend — the actual LLM cost from `meter()`, for instance. When
    omitted, the authorization's held values stand (the common SDK path). This
    is the hold-vs-settle split: `authorize` holds an estimate, `capture` books
    the truth.

    Takes no idempotency key, deliberately: `verdict.authorization_id` already
    is one. A repeated capture hits the backend's unique constraint on that
    column and replays the original transaction (200), so a retry after a lost
    response never double-books. A 409 here means something else entirely —
    capturing a hold that policy blocked, say.

    Args:
        verdict: The `Verdict` returned by `authorize()`.
        invoice_url: Optional evidence link attached to the transaction.
        amount: The actual settled amount (overrides the authorized estimate),
            as `Decimal`, `int`, or `str`. `float` is rejected.
        model: The actual model used (overrides the authorized value).
        tokens: The actual tokens consumed (overrides the authorized value).

    Returns:
        The booked `Transaction`.

    Raises:
        ContractError: The server's response could not be read as a
            `Transaction`.
        ConfigurationError: `amount` is a `float` or otherwise inexact, or this
            was called from a running event loop (use `capture_async`).
    """
    _forbid_running_loop("capture", "capture_async")
    call = _capture_call(
        verdict,
        invoice_url=invoice_url,
        amount=amount,
        model=model,
        tokens=tokens,
    )
    return _model(Transaction, _send(call), call.path)


async def capture_async(
    verdict: Verdict,
    *,
    invoice_url: str | None = None,
    amount: Money | None = None,
    model: str | None = None,
    tokens: int | None = None,
) -> Transaction:
    """Commit the spend approved by `verdict` to the ledger (async twin).

    Identical to `capture()` — same body, same `authorization_id` dedupe, same
    retry-safety.

    Args:
        verdict: The `Verdict` returned by `authorize_async()`.
        invoice_url: Optional evidence link attached to the transaction.
        amount: The actual settled amount (overrides the authorized estimate),
            as `Decimal`, `int`, or `str`. `float` is rejected.
        model: The actual model used (overrides the authorized value).
        tokens: The actual tokens consumed (overrides the authorized value).

    Returns:
        The booked `Transaction`.

    Raises:
        ContractError: The server's response could not be read as a
            `Transaction`.
        ConfigurationError: `amount` is a `float` or otherwise inexact.
    """
    call = _capture_call(
        verdict,
        invoice_url=invoice_url,
        amount=amount,
        model=model,
        tokens=tokens,
    )
    return _model(Transaction, await _send_async(call), call.path)


# --------------------------------------------------------------------------- #
# void
# --------------------------------------------------------------------------- #

_VOID_PATH = "/transactions/void"


def _void_call(verdict: Verdict, *, reason: str | None) -> _Call:
    """Build the void request for an approved-but-unspent hold."""
    body: dict[str, object] = {"authorization_id": verdict.authorization_id}
    if reason is not None:
        body["reason"] = reason
    return _Call(
        "POST",
        _VOID_PATH,
        json=body,
        # Safe to retry: the server returns the original release for an
        # already-voided hold rather than acting twice.
        retry_safe=True,
    )


def void(verdict: Verdict, *, reason: str | None = None) -> VoidResult:
    """Release an approved hold whose spend never happened (T5-4).

    An approved-but-uncaptured authorization reserves the agent's budget until it
    is captured or released. So a vendor call that fails *after* `authorize()`
    returned — or a crash between authorize and capture — permanently and
    silently shrinks what that agent may spend. Call this whenever you took a
    hold and then did not spend:

        verdict = lucnt.authorize(vendor="OpenAI", amount="2.00",
                                  currency="USD", reason="batch embed")
        try:
            result = do_the_purchase()
        except VendorError:
            lucnt.void(verdict, reason="vendor rejected the request")
            raise
        lucnt.capture(verdict, amount=result.actual_cost)

    `meter()` does this for you on its own error path.

    Idempotent and safe to retry: voiding an already-voided hold returns the
    original release with `already_released=True` rather than raising.

    Args:
        verdict: The `Verdict` returned by `authorize()`.
        reason: Why the spend did not happen, stored on the hold for the
            recovery inbox (M7).

    Returns:
        A `VoidResult` carrying when the hold was released.

    Raises:
        NotFoundError: No such authorization for this API key.
        ConflictError: The hold was already captured — the money moved, so it
            cannot be voided. Reverse the transaction instead.
        ContractError: The server's response could not be read as a `VoidResult`.
        ConfigurationError: Called from a running event loop (use `void_async`).
    """
    _forbid_running_loop("void", "void_async")
    call = _void_call(verdict, reason=reason)
    return _model(VoidResult, _send(call), call.path)


async def void_async(verdict: Verdict, *, reason: str | None = None) -> VoidResult:
    """Release an approved hold whose spend never happened (async twin of `void`).

    Args:
        verdict: The `Verdict` returned by `authorize_async()`.
        reason: Why the spend did not happen, stored on the hold for the
            recovery inbox (M7).

    Returns:
        A `VoidResult` carrying when the hold was released.

    Raises:
        NotFoundError: No such authorization for this API key.
        ConflictError: The hold was already captured — reverse it instead.
        ContractError: The server's response could not be read as a `VoidResult`.
    """
    call = _void_call(verdict, reason=reason)
    return _model(VoidResult, await _send_async(call), call.path)


# --------------------------------------------------------------------------- #
# spend
# --------------------------------------------------------------------------- #

def spend(
    *,
    vendor: str,
    amount: Money,
    reason: str,
    currency: str,
    agent: str | None = None,
    entity: str | None = None,
    intent: str | None = None,
    project: str | None = None,
    category: str | None = None,
    tax_category: str | None = None,
    tax_inclusive: bool = True,
    supply_type: Literal["service", "goods"] = "service",
    model: str | None = None,
    tokens: int | None = None,
    invoice_url: str | None = None,
    strict: bool = False,
    idempotency_key: str | None = None,
) -> Verdict | Transaction:
    """Authorize and, if approved, immediately capture — the atomic convenience.

    For flows with nothing to gate mid-way (no purchase step between authorize
    and capture). `strict=True` is forwarded to `authorize()`, so a non-approved
    verdict raises (`PolicyBlocked` / `BudgetExhausted`) instead of returning.

    Args:
        vendor: The merchant or vendor being paid.
        amount: The amount to spend, as `Decimal`, `int`, or `str`. `float` is
            rejected — money must be exact.
        reason: Human-readable justification, stored on the transaction.
        currency: ISO-4217 code of the currency actually being paid in.
        agent: Agent handle to attribute the spend to. Falls back to `task()`.
        entity: Legal entity handle. Falls back to `task()`.
        intent: Free-text purpose of the task. Falls back to `task()`.
        project: Cost-center / project label. Falls back to `task()`.
        category: Expense category. Inferred from the vendor when omitted.
        tax_category: Tax slab override. Standard rate if omitted.
        tax_inclusive: Whether a domestic `amount` already embeds tax.
        supply_type: `"service"` (default) or `"goods"`.
        model: Model identifier, for LLM/API spend only.
        tokens: Tokens consumed, for LLM/API spend only.
        invoice_url: Optional evidence link attached to the transaction.
        strict: Raise on a non-approved verdict instead of returning it.
        idempotency_key: Forwarded to the authorize step. If your code retries
            the whole call, wrap it in `lucnt.operation()` (or pass a key here)
            — otherwise the retry mints a second hold and captures it, which is
            the double-book T0-6 describes.

    Returns:
        The booked `Transaction` if approved. With `strict=False`, a blocking
        `Verdict` is returned unchanged; with `strict=True` it raises instead.

    Raises:
        ConfigurationError: `amount` is a `float` or otherwise inexact, or this
            was called from a running event loop (use `spend_async`).
    """
    _forbid_running_loop("spend", "spend_async")
    verdict = authorize(
        vendor=vendor,
        amount=amount,
        currency=currency,
        reason=reason,
        agent=agent,
        entity=entity,
        intent=intent,
        project=project,
        category=category,
        tax_category=tax_category,
        tax_inclusive=tax_inclusive,
        supply_type=supply_type,
        model=model,
        tokens=tokens,
        strict=strict,
        idempotency_key=idempotency_key,
    )
    if not verdict.approved:
        return verdict
    return capture(verdict, invoice_url=invoice_url)


async def spend_async(
    *,
    vendor: str,
    amount: Money,
    reason: str,
    currency: str,
    agent: str | None = None,
    entity: str | None = None,
    intent: str | None = None,
    project: str | None = None,
    category: str | None = None,
    tax_category: str | None = None,
    tax_inclusive: bool = True,
    supply_type: Literal["service", "goods"] = "service",
    model: str | None = None,
    tokens: int | None = None,
    invoice_url: str | None = None,
    strict: bool = False,
    idempotency_key: str | None = None,
) -> Verdict | Transaction:
    """Authorize and, if approved, capture — the atomic convenience (async twin).

    Two awaits, not one: a blocking verdict short-circuits before the capture
    round trip, exactly as in `spend()`.

    Takes the same arguments as `spend()`; see there for what each means.

    Returns:
        The booked `Transaction` if approved. With `strict=False`, a blocking
        `Verdict` is returned unchanged; with `strict=True` it raises instead.

    Raises:
        ConfigurationError: `amount` is a `float` or otherwise inexact.
    """
    verdict = await authorize_async(
        vendor=vendor,
        amount=amount,
        currency=currency,
        reason=reason,
        agent=agent,
        entity=entity,
        intent=intent,
        project=project,
        category=category,
        tax_category=tax_category,
        tax_inclusive=tax_inclusive,
        supply_type=supply_type,
        model=model,
        tokens=tokens,
        strict=strict,
        idempotency_key=idempotency_key,
    )
    if not verdict.approved:
        return verdict
    return await capture_async(verdict, invoice_url=invoice_url)


# --------------------------------------------------------------------------- #
# check_budget
# --------------------------------------------------------------------------- #

_BUDGETS_PATH = "/budgets"


def _check_budget_call(agent: str | None) -> tuple[_Call, str]:
    """Build the budget read, returning the call and the resolved agent slug."""
    ctx = get_current_task()
    slug = _resolve_required(agent, ctx.agent if ctx else None, "agent")
    # Filter server-side: `GET /budgets` is paginated, so scanning the returned
    # array would miss any agent past the first page and report "no budget" for
    # an agent that exists.
    return _Call("GET", _BUDGETS_PATH, params={"agent": slug, "limit": "1"}), slug


def _check_budget_result(
    payload: dict[str, object], slug: str, path: str
) -> BudgetStatus:
    """Read the single matching budget row, or say why it could not be read."""
    rows = payload.get("agents")
    if not isinstance(rows, list):
        raise ContractError(
            f"{path} response has no 'agents' list "
            f"(got {type(rows).__name__}) — the backend may be newer than the SDK.",
            path=path,
        )
    if not rows:
        raise LucntError(f"no budget found for agent {slug!r}")
    row = rows[0]
    if not isinstance(row, dict):
        raise ContractError(
            f"{path} returned an 'agents' entry that is not an object "
            f"(got {type(row).__name__}).",
            path=path,
        )
    try:
        return BudgetStatus(
            agent=slug,
            monthly_budget=row["monthly_budget"],
            spent=row["spent"],
            remaining=row["remaining"],
            pct_used=row["pct_used"],
            status=row["status"],
        )
    except (KeyError, ValidationError) as exc:
        # A KeyError escaping here would be indistinguishable from a bug in the
        # SDK; it is in fact the backend having renamed or dropped a field.
        raise ContractError(
            f"{path} returned a budget row this lucnt version cannot read "
            f"({exc!r}) — the backend may be newer than the SDK.",
            path=path,
        ) from exc


def check_budget(*, agent: str | None = None) -> BudgetStatus:
    """Read an agent's live budget utilisation (M8 — self-regulation).

    `agent` falls back to the ambient `task()` context when not passed.

    Returns:
        A `BudgetStatus` for the agent (monthly budget, spend, remaining,
        pct used, and an `ok`/`warning`/`exhausted` label).

    Raises:
        ConfigurationError: No agent given and none set via `task()`, or this
            was called from a running event loop (use `check_budget_async`).
        LucntError: No budget row matches the agent for this API key.
        ContractError: The response did not carry a readable budget row.
    """
    _forbid_running_loop("check_budget", "check_budget_async")
    call, slug = _check_budget_call(agent)
    return _check_budget_result(_send(call), slug, call.path)


async def check_budget_async(*, agent: str | None = None) -> BudgetStatus:
    """Read an agent's live budget utilisation (async twin of `check_budget`).

    Returns:
        A `BudgetStatus` for the agent.

    Raises:
        ConfigurationError: No agent given and none set via `task()`.
        LucntError: No budget row matches the agent for this API key.
        ContractError: The response did not carry a readable budget row.
    """
    call, slug = _check_budget_call(agent)
    return _check_budget_result(await _send_async(call), slug, call.path)


# --------------------------------------------------------------------------- #
# query
# --------------------------------------------------------------------------- #

_QUERY_PATH = "/intelligence/query"


def _query_call(agent: str | None, question: str | None) -> _Call:
    """Build the financial-memory read for one agent."""
    ctx = get_current_task()
    slug = _resolve_required(agent, ctx.agent if ctx else None, "agent")
    params: dict[str, str] = {"agent": slug}
    if question is not None:
        params["q"] = question
    return _Call("GET", _QUERY_PATH, params=params)


def query(
    *,
    agent: str | None = None,
    question: str | None = None,
) -> FinancialMemory:
    """Read an agent's living financial memory (M8 — self-regulation).

    Lets an agent check itself *before* it spends: budget headroom, a month-end
    run-rate projection, cost-per-output, and its spend fingerprint. `agent`
    falls back to the ambient `task()` context. `question` (e.g. "am I on track
    this month?") is answered in `FinancialMemory.headline`, keyword-routed
    server-side to the most relevant dimension — there is no LLM, so the full
    structured memory is always returned regardless of the wording.

    **Freshness.** `budget` is always computed live from the ledger. The
    fingerprint statistics (mean, stddev, median, top vendors, anomaly score)
    are served from the agent's last stored fingerprint once one exists, so they
    are as fresh as the most recent server-side recompute; an agent that has
    never been recomputed gets them computed live. Gate spending decisions on
    `budget`, which never goes stale.

    Args:
        agent: Agent slug to read; falls back to `task()` context.
        question: Optional free-text question, routed to `headline`.

    Returns:
        A `FinancialMemory` for the agent.

    Raises:
        ConfigurationError: No agent given and none set via `task()`, or this
            was called from a running event loop (use `query_async`).
        NotFoundError: The agent is unknown for this API key (404).
        ContractError: The response could not be read as a `FinancialMemory`.
    """
    _forbid_running_loop("query", "query_async")
    call = _query_call(agent, question)
    return _model(FinancialMemory, _send(call), call.path)


async def query_async(
    *,
    agent: str | None = None,
    question: str | None = None,
) -> FinancialMemory:
    """Read an agent's living financial memory (async twin of `query`).

    Same freshness caveat as `query()`: gate spending decisions on `budget`,
    which is always live, rather than on the fingerprint statistics, which are
    as fresh as the last server-side recompute.

    Args:
        agent: Agent slug to read; falls back to `task()` context.
        question: Optional free-text question, routed to `headline`.

    Returns:
        A `FinancialMemory` for the agent.

    Raises:
        ConfigurationError: No agent given and none set via `task()`.
        NotFoundError: The agent is unknown for this API key (404).
        ContractError: The response could not be read as a `FinancialMemory`.
    """
    call = _query_call(agent, question)
    return _model(FinancialMemory, await _send_async(call), call.path)


# --------------------------------------------------------------------------- #
# mark_failed_delivery
# --------------------------------------------------------------------------- #

_FAILED_DELIVERY_PATH = "/recon/failed-delivery"


def _resolve_transaction_id(
    transaction: Transaction | None, transaction_id: str | None
) -> str:
    """Take the id from `transaction` or `transaction_id`; error if neither is set."""
    if transaction is not None:
        return transaction.id
    if transaction_id is not None:
        return transaction_id
    raise ConfigurationError(
        "mark_failed_delivery() requires 'transaction' or 'transaction_id'."
    )


def _failed_delivery_call(
    transaction: Transaction | None,
    transaction_id: str | None,
    reason: str,
    amount_at_stake: Money | None,
) -> _Call:
    """Build the failed-delivery report for a committed transaction."""
    tx_id = _resolve_transaction_id(transaction, transaction_id)
    body: dict[str, object] = {"transaction_id": tx_id, "reason": reason}
    if amount_at_stake is not None:
        body["amount_at_stake"] = str(_as_decimal(amount_at_stake, "amount_at_stake"))
    return _Call(
        "POST",
        _FAILED_DELIVERY_PATH,
        json=body,
        # Safe to retry: the server dedupes the exception per transaction (P4).
        retry_safe=True,
    )


def mark_failed_delivery(
    *,
    transaction: Transaction | None = None,
    transaction_id: str | None = None,
    reason: str,
    amount_at_stake: Money | None = None,
) -> ReconException:
    """Flag a committed spend whose downstream task failed (Phase 4 recovery, M7).

    Some spend cannot be judged from ledger data alone — the agent paid but the
    task errored (a corrupt dataset, an undelivered service). This reports that
    to the recovery layer, which opens a `failed_delivery` exception and drafts a
    refund request. Pass the `Transaction` returned by `capture()`/`spend()`, or
    its id.

    Args:
        transaction: The committed `Transaction` whose task failed.
        transaction_id: The transaction id, if the object isn't at hand.
        reason: Human-readable description of the failure (stored as evidence).
        amount_at_stake: Recoverable amount as `Decimal`, `int`, or `str`;
            defaults server-side to the transaction's full paid amount when
            omitted. `float` is rejected.

    Returns:
        The created (or, on a repeat call, existing) `ReconException` — the
        server dedupes an active failed-delivery per transaction.

    Raises:
        ConfigurationError: Neither `transaction` nor `transaction_id` given,
            `amount_at_stake` is a `float`, or this was called from a running
            event loop (use `mark_failed_delivery_async`).
        ContractError: The response could not be read as a `ReconException`.
    """
    _forbid_running_loop("mark_failed_delivery", "mark_failed_delivery_async")
    call = _failed_delivery_call(
        transaction, transaction_id, reason, amount_at_stake
    )
    return _model(ReconException, _send(call), call.path)


async def mark_failed_delivery_async(
    *,
    transaction: Transaction | None = None,
    transaction_id: str | None = None,
    reason: str,
    amount_at_stake: Money | None = None,
) -> ReconException:
    """Flag a committed spend whose downstream task failed (async twin).

    Args:
        transaction: The committed `Transaction` whose task failed.
        transaction_id: The transaction id, if the object isn't at hand.
        reason: Human-readable description of the failure (stored as evidence).
        amount_at_stake: Recoverable amount as `Decimal`, `int`, or `str`;
            defaults server-side to the full paid amount. `float` is rejected.

    Returns:
        The created (or existing) `ReconException`.

    Raises:
        ConfigurationError: Neither `transaction` nor `transaction_id` given, or
            `amount_at_stake` is a `float`.
        ContractError: The response could not be read as a `ReconException`.
    """
    call = _failed_delivery_call(
        transaction, transaction_id, reason, amount_at_stake
    )
    return _model(ReconException, await _send_async(call), call.path)
