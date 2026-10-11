"""authorize() / capture() / spend(): the universal spend-type-agnostic primitive.

Sync and `_async` twins build the same `_Call`, so they cannot disagree on the
wire contract. Sync calls raise inside a running event loop instead of blocking.
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
# Pools displaced by a reconfigure; closed at exit, never inline.
_retired_transports: list[Transport] = []

_async_lock = threading.Lock()
# Per event loop (async pools are loop-bound); weak keys so dead loops drop out.
_async_transports: WeakKeyDictionary[
    asyncio.AbstractEventLoop, tuple[AsyncTransport, LucntConfig]
] = WeakKeyDictionary()


def _get_transport() -> Transport:
    """Return the process-wide pooled Transport, rebuilding it if reconfigured.

    Config is compared by value. A displaced pool is retired, not closed:
    closing it could tear a request in flight on another thread.
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
    """Refuse a blocking call from inside an event loop (it would stall the loop)."""
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

    A displaced pool is dropped, not closed: closing needs an `await`.
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

    Optional; await before loop shutdown to release sockets deterministically.
    A no-op if this loop never made an async call.
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
        with contextlib.suppress(Exception):
            pool.close()


@dataclass(frozen=True)
class _Call:
    """One HTTP call, fully described but not yet made (shared by sync/async)."""

    method: str
    path: str
    json: dict[str, object] | None = None
    params: dict[str, str] | None = None
    idempotency_key: str | None = None
    retry_safe: bool = False


def _payload(response: httpx.Response, path: str) -> dict[str, object]:
    """Read a success body as a JSON object, or raise `ContractError`."""
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
    """Validate a response body into `model`, or raise `ContractError`."""
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
    """Normalize an exact money input to `Decimal`, rejecting `float` and `bool`."""
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
    """Mint a key for one authorize; covers transport retries, not caller retries."""
    return uuid.uuid4().hex


# Cosmetic fields that cannot change the verdict or booked money. Deny-list, so
# new `SpendRequest` fields are fingerprinted by default (fail-safe).
_FINGERPRINT_EXCLUDED = frozenset({"intent", "model", "project", "reason", "tokens"})


def _operation_fingerprint(request: SpendRequest) -> dict[str, object]:
    """Reduce a request to the fields that decide which spend it is.

    Python-mode dump keeps `amount` a `Decimal`, so `10 == "10.00"`.
    """
    return {
        field: value
        for field, value in request.model_dump().items()
        if field not in _FINGERPRINT_EXCLUDED
    }


def _resolve_idempotency_key(explicit: str | None, request: SpendRequest) -> str:
    """Pick the key: explicit (skips the guardrail), then `operation()`, then auto."""
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

        verdict = lucnt.authorize(vendor="Acme", amount="49.00", currency="USD",
                                  reason="seat upgrade")
        if verdict.approved:
            ...  # pay, then lucnt.capture(verdict)

    Args:
        vendor: The merchant or vendor being paid.
        amount: Amount to hold as `Decimal`, `int`, or `str` (`float` rejected).
        reason: Human-readable justification, stored on the transaction.
        currency: ISO-4217 code actually paid in; never defaulted.
        agent: Agent handle; falls back to `task()`.
        entity: Legal entity handle; falls back to `task()`.
        intent: Free-text purpose; falls back to `task()`.
        project: Cost-center / project label; falls back to `task()`.
        category: `software|ai_compute|travel|marketing|general`; else inferred.
        tax_category: Tax slab override, e.g. `"gst_5"`.
        tax_inclusive: Whether a domestic `amount` already embeds tax.
        supply_type: `"service"` or `"goods"` (cross-border goods use customs).
        model: Model identifier, for LLM/API spend only.
        tokens: Tokens consumed, for LLM/API spend only.
        strict: Raise on a non-approved verdict instead of returning it.
        idempotency_key: Overrides the `operation()` scope and auto-generated key.

    Returns:
        A `Verdict`; when not strict, check `.approved` before spending.

    Raises:
        PolicyBlocked: `strict=True` and the verdict was not approved.
        BudgetExhausted: `strict=True` and the block was a budget cap.
        IdempotencyConflict: The `operation()` scope holds a different spend.
        ContractError: The response could not be read as a `Verdict`.
        ConfigurationError: Inexact amount, bad key, unresolvable agent/entity,
            or called from a running event loop.
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
    """Async `authorize()`; same arguments, returns, and raises."""
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
    estimated: bool,
) -> _Call:
    """Build the capture request, overriding only the values actually settled."""
    body: dict[str, object] = {
        "authorization_id": verdict.authorization_id,
        "source": "sdk",
        "invoice_url": invoice_url,
    }
    if amount is not None:
        body["amount"] = str(_as_decimal(amount))
    if model is not None:
        body["model"] = model
    if tokens is not None:
        body["tokens"] = tokens
    if estimated:
        # Only sent when True, so older backends never see an unknown field.
        body["estimated"] = True
    return _Call(
        "POST",
        _CAPTURE_PATH,
        json=body,
        # Backend dedupes on authorization_id, so a retry replays.
        retry_safe=True,
    )


def capture(
    verdict: Verdict,
    *,
    invoice_url: str | None = None,
    amount: Money | None = None,
    model: str | None = None,
    tokens: int | None = None,
    estimated: bool = False,
) -> Transaction:
    """Commit the spend approved by `verdict` to the ledger.

    Retry-safe without a key: a repeat replays the original transaction.

        tx = lucnt.capture(verdict, amount="47.12", invoice_url=url)

    Args:
        verdict: The `Verdict` returned by `authorize()`.
        invoice_url: Optional evidence link attached to the transaction.
        amount: Actual settled amount, overriding the hold (`float` rejected).
        model: Actual model used, overriding the authorized value.
        tokens: Actual tokens consumed, overriding the authorized value.
        estimated: Mark the booked amount as an unsettled estimate.

    Returns:
        The booked `Transaction`.

    Raises:
        ContractError: The response could not be read as a `Transaction`.
        ConfigurationError: Inexact amount, or called from a running event loop.
    """
    _forbid_running_loop("capture", "capture_async")
    call = _capture_call(
        verdict,
        invoice_url=invoice_url,
        amount=amount,
        model=model,
        tokens=tokens,
        estimated=estimated,
    )
    return _model(Transaction, _send(call), call.path)


async def capture_async(
    verdict: Verdict,
    *,
    invoice_url: str | None = None,
    amount: Money | None = None,
    model: str | None = None,
    tokens: int | None = None,
    estimated: bool = False,
) -> Transaction:
    """Async `capture()`; same arguments, returns, and raises."""
    call = _capture_call(
        verdict,
        invoice_url=invoice_url,
        amount=amount,
        model=model,
        tokens=tokens,
        estimated=estimated,
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
        # A repeat void returns the original release.
        retry_safe=True,
    )


def void(verdict: Verdict, *, reason: str | None = None) -> VoidResult:
    """Release an approved hold whose spend never happened.

    An uncaptured hold keeps reserving budget, so void it whenever the
    purchase fails after `authorize()`. Idempotent.

    Args:
        verdict: The `Verdict` returned by `authorize()`.
        reason: Why the spend did not happen, stored on the hold.

    Returns:
        A `VoidResult` carrying when the hold was released.

    Raises:
        NotFoundError: No such authorization for this API key.
        ConflictError: The hold was already captured; reverse it instead.
        ContractError: The response could not be read as a `VoidResult`.
        ConfigurationError: Called from a running event loop.
    """
    _forbid_running_loop("void", "void_async")
    call = _void_call(verdict, reason=reason)
    return _model(VoidResult, _send(call), call.path)


async def void_async(verdict: Verdict, *, reason: str | None = None) -> VoidResult:
    """Async `void()`; same arguments, returns, and raises."""
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
    """Authorize and, if approved, immediately capture.

        lucnt.spend(vendor="Acme", amount="49.00", currency="USD",
                    reason="seat upgrade", strict=True)

    Takes the arguments of `authorize()`, plus `invoice_url` for the capture.
    If your code retries the whole call, wrap it in `lucnt.operation()`.

    Returns:
        The booked `Transaction`, or the blocking `Verdict` when not strict.

    Raises:
        PolicyBlocked: `strict=True` and the verdict was not approved.
        BudgetExhausted: `strict=True` and the block was a budget cap.
        ConfigurationError: Inexact amount, or called from a running event loop.
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
    """Async `spend()`; same arguments, returns, and raises."""
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
    # Filter server-side: `GET /budgets` is paginated.
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
            # Required, not defaulted: missing holds would overstate headroom.
            reserved=row["reserved"],
            available=row["available"],
            pct_used=row["pct_used"],
            status=row["status"],
        )
    except (KeyError, ValidationError) as exc:
        raise ContractError(
            f"{path} returned a budget row this lucnt version cannot read "
            f"({exc!r}) — the backend may be newer than the SDK.",
            path=path,
        ) from exc


def check_budget(*, agent: str | None = None) -> BudgetStatus:
    """Read an agent's live budget utilisation.

    Args:
        agent: Agent slug; falls back to `task()`.

    Returns:
        A `BudgetStatus`; self-regulate on `.available`.

    Raises:
        ConfigurationError: No agent resolvable, or called from a running loop.
        LucntError: No budget row matches the agent for this API key.
        ContractError: The response did not carry a readable budget row.
    """
    _forbid_running_loop("check_budget", "check_budget_async")
    call, slug = _check_budget_call(agent)
    return _check_budget_result(_send(call), slug, call.path)


async def check_budget_async(*, agent: str | None = None) -> BudgetStatus:
    """Async `check_budget()`; same arguments, returns, and raises."""
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
    """Read an agent's living financial memory.

    `budget` is always live; fingerprint stats may lag the last server
    recompute, so gate spending on `budget`.

    Args:
        agent: Agent slug; falls back to `task()`.
        question: Optional free-text question, answered in `headline`.

    Returns:
        A `FinancialMemory` for the agent.

    Raises:
        ConfigurationError: No agent resolvable, or called from a running loop.
        NotFoundError: The agent is unknown for this API key.
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
    """Async `query()`; same arguments, returns, and raises."""
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
        # The server dedupes the exception per transaction.
        retry_safe=True,
    )


def mark_failed_delivery(
    *,
    transaction: Transaction | None = None,
    transaction_id: str | None = None,
    reason: str,
    amount_at_stake: Money | None = None,
) -> ReconException:
    """Flag a committed spend whose downstream task failed, for recovery.

    Args:
        transaction: The committed `Transaction` whose task failed.
        transaction_id: The transaction id, if the object isn't at hand.
        reason: Description of the failure, stored as evidence.
        amount_at_stake: Recoverable amount; defaults to the full paid amount.

    Returns:
        The created (or, on repeat, existing) `ReconException`.

    Raises:
        ConfigurationError: No transaction given, inexact amount, or called
            from a running event loop.
        ContractError: The response could not be read as a `ReconException`.
    """
    _forbid_running_loop("mark_failed_delivery", "mark_failed_delivery_async")
    call = _failed_delivery_call(transaction, transaction_id, reason, amount_at_stake)
    return _model(ReconException, _send(call), call.path)


async def mark_failed_delivery_async(
    *,
    transaction: Transaction | None = None,
    transaction_id: str | None = None,
    reason: str,
    amount_at_stake: Money | None = None,
) -> ReconException:
    """Async `mark_failed_delivery()`; same arguments, returns, and raises."""
    call = _failed_delivery_call(transaction, transaction_id, reason, amount_at_stake)
    return _model(ReconException, await _send_async(call), call.path)
