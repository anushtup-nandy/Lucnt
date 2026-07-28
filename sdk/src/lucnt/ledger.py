"""authorize() / capture() / spend() — the universal spend-type-agnostic primitive.

Every agent-initiated spend flows through this pair: `authorize()` lets
policy block *before* money moves; `capture()` commits the real spend to
the ledger. `spend()` is the atomic convenience for callers with nothing to
gate mid-way. Spend-type-agnostic: a flight, a SaaS seat, and a token batch
all flow through the same primitive (`model`/`tokens` are optional, for the
API/LLM subset only).
"""

from __future__ import annotations

import atexit
import contextlib
import threading
import uuid
from decimal import Decimal
from typing import Literal

from lucnt.client import Transport, check_idempotency_key
from lucnt.config import get_config
from lucnt.context import get_current_operation, get_current_task
from lucnt.exceptions import (
    BudgetExhausted,
    ConfigurationError,
    LucntError,
    PolicyBlocked,
)
from lucnt.models import (
    BudgetStatus,
    FinancialMemory,
    LucntConfig,
    ReconException,
    SpendRequest,
    Transaction,
    Verdict,
    VoidResult,
)

_BUDGET_EXHAUSTED_CODE = "budget_exhausted"

_transport_lock = threading.Lock()
_transport: Transport | None = None
_transport_config: LucntConfig | None = None
# Pools displaced by a genuine reconfigure. Never closed inline — see below.
_retired_transports: list[Transport] = []


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


@atexit.register
def _close_transports() -> None:
    """Close the live pool and every retired one at interpreter exit."""
    with _transport_lock:
        pools = [*_retired_transports, *([_transport] if _transport else [])]
        _retired_transports.clear()
    for pool in pools:
        # Never raise during interpreter exit — a failed close here would
        # surface as a confusing shutdown traceback and hide the real work.
        with contextlib.suppress(Exception):
            pool.close()


def _resolve_required(explicit: str | None, ambient: str | None, field: str) -> str:
    """Prefer an explicit arg over ambient task() context; error if neither is set."""
    value = explicit if explicit is not None else ambient
    if value is None:
        raise ConfigurationError(
            f"'{field}' is required: pass it explicitly or set it via lucnt.task()."
        )
    return value


def _as_decimal(amount: Decimal | float | int | str) -> Decimal:
    """Normalize numeric input to Decimal via its string form.

    Avoids `Decimal(float)`'s binary precision explosion (`Decimal(1.83)` is
    not `Decimal("1.83")`) while keeping plain `amount=1.83` call sites exact.
    """
    return amount if isinstance(amount, Decimal) else Decimal(str(amount))


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


def authorize(
    *,
    vendor: str,
    amount: Decimal | float | int | str,
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
        amount: The amount to hold. Pass `Decimal` or `str` for money —
            `float` is accepted today but will be rejected in 1.0.0.
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
        ConfigurationError: `idempotency_key` is blank or over 255 characters,
            or `agent`/`entity` is resolvable from neither argument nor `task()`.
    """
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
    response = _get_transport().request(
        "POST",
        "/transactions/authorize",
        json=request.model_dump(mode="json"),
        idempotency_key=_resolve_idempotency_key(idempotency_key, request),
    )
    verdict = Verdict.model_validate(response.json())
    if strict and not verdict.approved:
        _raise_for_verdict(verdict)
    return verdict


def capture(
    verdict: Verdict,
    *,
    invoice_url: str | None = None,
    amount: Decimal | float | int | str | None = None,
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
        amount: The actual settled amount (overrides the authorized estimate).
        model: The actual model used (overrides the authorized value).
        tokens: The actual tokens consumed (overrides the authorized value).

    Returns:
        The booked `Transaction`.
    """
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
    response = _get_transport().request(
        "POST",
        "/transactions/capture",
        json=body,
        # Safe to retry without a key: the backend dedupes on the
        # authorization_id unique constraint and replays the original
        # transaction, so a repeat can never double-book.
        retry_safe=True,
    )
    return Transaction.model_validate(response.json())


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
    """
    body: dict[str, object] = {"authorization_id": verdict.authorization_id}
    if reason is not None:
        body["reason"] = reason
    response = _get_transport().request(
        "POST",
        "/transactions/void",
        json=body,
        # Safe to retry: the server returns the original release for an
        # already-voided hold rather than acting twice.
        retry_safe=True,
    )
    return VoidResult.model_validate(response.json())


def spend(
    *,
    vendor: str,
    amount: Decimal | float | int | str,
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
        amount: The amount to spend. Prefer `Decimal` or `str` — `float` is
            accepted today but will be rejected in 1.0.0.
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
        idempotency_key: Forwarded to `authorize()`. If your code retries
            `spend()` itself, wrap it in `lucnt.operation()` (or pass a key
            here) — otherwise the retry mints a second hold and captures it,
            which is the double-book T0-6 describes.

    Returns:
        The booked `Transaction` if approved. With `strict=False`, a blocking
        `Verdict` is returned unchanged; with `strict=True` it raises instead.
    """
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


def check_budget(*, agent: str | None = None) -> BudgetStatus:
    """Read an agent's live budget utilisation (M8 — self-regulation).

    `agent` falls back to the ambient `task()` context when not passed.

    Returns:
        A `BudgetStatus` for the agent (monthly budget, spend, remaining,
        pct used, and an `ok`/`warning`/`exhausted` label).

    Raises:
        ConfigurationError: No agent given and none set via `task()`.
        LucntError: No budget row matches the agent for this API key.
    """
    ctx = get_current_task()
    slug = _resolve_required(agent, ctx.agent if ctx else None, "agent")
    # Filter server-side: `GET /budgets` is paginated, so scanning the returned
    # array would miss any agent past the first page and report "no budget" for
    # an agent that exists.
    response = _get_transport().request(
        "GET", "/budgets", params={"agent": slug, "limit": "1"}
    )
    rows = response.json()["agents"]
    if not rows:
        raise LucntError(f"no budget found for agent {slug!r}")
    row = rows[0]
    return BudgetStatus(
        agent=slug,
        monthly_budget=row["monthly_budget"],
        spent=row["spent"],
        remaining=row["remaining"],
        pct_used=row["pct_used"],
        status=row["status"],
    )


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
        ConfigurationError: No agent given and none set via `task()`.
        LucntError: The agent is unknown for this API key (404).
    """
    ctx = get_current_task()
    slug = _resolve_required(agent, ctx.agent if ctx else None, "agent")
    params: dict[str, str] = {"agent": slug}
    if question is not None:
        params["q"] = question
    response = _get_transport().request("GET", "/intelligence/query", params=params)
    return FinancialMemory.model_validate(response.json())


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


def mark_failed_delivery(
    *,
    transaction: Transaction | None = None,
    transaction_id: str | None = None,
    reason: str,
    amount_at_stake: Decimal | float | int | str | None = None,
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
        amount_at_stake: Recoverable amount; defaults server-side to the
            transaction's full paid amount when omitted.

    Returns:
        The created (or, on a repeat call, existing) `ReconException` — the
        server dedupes an active failed-delivery per transaction.

    Raises:
        ConfigurationError: Neither `transaction` nor `transaction_id` given.
    """
    tx_id = _resolve_transaction_id(transaction, transaction_id)
    body: dict[str, object] = {"transaction_id": tx_id, "reason": reason}
    if amount_at_stake is not None:
        body["amount_at_stake"] = str(_as_decimal(amount_at_stake))
    response = _get_transport().request(
        "POST",
        "/recon/failed-delivery",
        json=body,
        # Safe to retry: the server dedupes the exception per transaction (P4).
        retry_safe=True,
    )
    return ReconException.model_validate(response.json())
