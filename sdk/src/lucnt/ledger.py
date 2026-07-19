"""authorize() / capture() / spend() — the universal spend-type-agnostic primitive.

Every agent-initiated spend flows through this pair: `authorize()` lets
policy block *before* money moves; `capture()` commits the real spend to
the ledger. `spend()` is the atomic convenience for callers with nothing to
gate mid-way. Spend-type-agnostic: a flight, a SaaS seat, and a token batch
all flow through the same primitive (`model`/`tokens` are optional, for the
API/LLM subset only).
"""

from __future__ import annotations

from decimal import Decimal

from lucnt.client import Transport
from lucnt.config import get_config
from lucnt.context import get_current_task
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
)

_BUDGET_EXHAUSTED_CODE = "budget_exhausted"

_transport: Transport | None = None
_transport_config: LucntConfig | None = None


def _get_transport() -> Transport:
    """Return the process-wide pooled Transport, rebuilding it if reconfigured."""
    global _transport, _transport_config
    config = get_config()
    if _transport is None or _transport_config is not config:
        if _transport is not None:
            _transport.close()
        _transport = Transport(config)
        _transport_config = config
    return _transport


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
    model: str | None = None,
    tokens: int | None = None,
    strict: bool = False,
) -> Verdict:
    """Ask policy for a verdict on a spend, before any money moves.

    `agent`/`entity`/`intent`/`project` fall back to the ambient `task()`
    context when not passed explicitly. With `strict=True`, a non-approved
    verdict raises instead of returning — `BudgetExhausted` if the block was a
    budget cap, else `PolicyBlocked` (which `BudgetExhausted` subclasses).

    Returns:
        A `Verdict`. With `strict=False` (default) a blocking verdict is
        returned, never raised — always check `.approved` before spending.

    Raises:
        PolicyBlocked: `strict=True` and the verdict was not approved.
        BudgetExhausted: `strict=True` and the block was a budget cap.
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
        model=model,
        tokens=tokens,
    )
    response = _get_transport().request(
        "POST", "/transactions/authorize", json=request.model_dump(mode="json")
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
        "POST", "/transactions/capture", json=body
    )
    return Transaction.model_validate(response.json())


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
    model: str | None = None,
    tokens: int | None = None,
    invoice_url: str | None = None,
    strict: bool = False,
) -> Verdict | Transaction:
    """Authorize and, if approved, immediately capture — the atomic convenience.

    For flows with nothing to gate mid-way (no purchase step between authorize
    and capture). `strict=True` is forwarded to `authorize()`, so a non-approved
    verdict raises (`PolicyBlocked` / `BudgetExhausted`) instead of returning.

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
        model=model,
        tokens=tokens,
        strict=strict,
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
    response = _get_transport().request("GET", "/budgets")
    for row in response.json()["agents"]:
        if row["slug"] == slug:
            return BudgetStatus(
                agent=slug,
                monthly_budget=row["monthly_budget"],
                spent=row["spent"],
                remaining=row["remaining"],
                pct_used=row["pct_used"],
                status=row["status"],
            )
    raise LucntError(f"no budget found for agent {slug!r}")


def query(
    *,
    agent: str | None = None,
    question: str | None = None,
) -> FinancialMemory:
    """Read an agent's living financial memory (M8 — self-regulation).

    Computed live from the ledger, so an agent can check itself *before* it
    spends: budget headroom, a month-end run-rate projection, cost-per-output,
    and its spend fingerprint. `agent` falls back to the ambient `task()`
    context. `question` (e.g. "am I on track this month?") is answered in
    `FinancialMemory.headline`, keyword-routed server-side to the most relevant
    dimension — there is no LLM, so the full structured memory is always
    returned regardless of the wording.

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
        "POST", "/recon/failed-delivery", json=body
    )
    return ReconException.model_validate(response.json())
