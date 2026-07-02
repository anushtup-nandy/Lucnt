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
from lucnt.exceptions import ConfigurationError
from lucnt.models import LucntConfig, SpendRequest, Transaction, Verdict

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


def authorize(
    *,
    vendor: str,
    amount: Decimal | float | int | str,
    reason: str,
    currency: str | None = None,
    agent: str | None = None,
    entity: str | None = None,
    intent: str | None = None,
    project: str | None = None,
    model: str | None = None,
    tokens: int | None = None,
) -> Verdict:
    """Ask policy for a verdict on a spend, before any money moves.

    `agent`/`entity`/`intent`/`project` fall back to the ambient `task()`
    context when not passed explicitly.

    Returns:
        A `Verdict`. Always check `.approved` before spending — a blocking
        verdict is returned here, never raised. `strict=True` raising lands
        in P3 once the policy engine differentiates block reasons.
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
        model=model,
        tokens=tokens,
    )
    response = _get_transport().request(
        "POST", "/transactions/authorize", json=request.model_dump(mode="json")
    )
    return Verdict.model_validate(response.json())


def capture(verdict: Verdict, *, invoice_url: str | None = None) -> Transaction:
    """Commit the spend approved by `verdict` to the ledger.

    Args:
        verdict: The `Verdict` returned by `authorize()`.
        invoice_url: Optional evidence link attached to the transaction.

    Returns:
        The booked `Transaction`.
    """
    response = _get_transport().request(
        "POST",
        "/transactions/capture",
        json={
            "authorization_id": verdict.authorization_id,
            "source": "sdk",
            "invoice_url": invoice_url,
        },
    )
    return Transaction.model_validate(response.json())


def spend(
    *,
    vendor: str,
    amount: Decimal | float | int | str,
    reason: str,
    currency: str | None = None,
    agent: str | None = None,
    entity: str | None = None,
    intent: str | None = None,
    project: str | None = None,
    model: str | None = None,
    tokens: int | None = None,
    invoice_url: str | None = None,
) -> Verdict | Transaction:
    """Authorize and, if approved, immediately capture — the atomic convenience.

    For flows with nothing to gate mid-way (no purchase step between
    authorize and capture).

    Returns:
        The booked `Transaction` if approved, otherwise the blocking
        `Verdict` unchanged — check `.approved` on the result either way.
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
        model=model,
        tokens=tokens,
    )
    if not verdict.approved:
        return verdict
    return capture(verdict, invoice_url=invoice_url)
