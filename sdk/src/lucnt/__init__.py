"""lucnt: financial infrastructure that attaches to your existing agents.

Public surface: `configure()`, `task()`, `operation()`, `authorize()`,
`capture()`, `spend()`
— the universal spend-type-agnostic primitive — plus `check_budget()` (Phase 3
self-regulation read), `meter()` (Phase 3.5 auto-instrumentation for LLM/vendor
clients), `mark_failed_delivery()` (Phase 4 recovery hook), and `query()` (Phase 5
living financial memory), their typed models, and the SDK's exception hierarchy.

**Every call has an `_async` twin** (T5-6) — `spend_async()`, `authorize_async()`,
`capture_async()`, `void_async()`, `check_budget_async()`, `query_async()`,
`mark_failed_delivery_async()` — plus `aclose()` for deterministic shutdown.
`meter()` detects an async vendor client and returns an async-aware proxy, so
the same call wraps `OpenAI()` and `AsyncOpenAI()`. Calling a synchronous
function from inside a running event loop raises rather than stalling the loop.
"""

from __future__ import annotations

from lucnt._version import __version__
from lucnt.config import configure
from lucnt.context import bind_context, operation, task
from lucnt.exceptions import (
    APIError,
    AuthError,
    BudgetExhausted,
    ConfigurationError,
    ConflictError,
    ContractError,
    IdempotencyConflict,
    InvalidRequestError,
    LucntError,
    NotFoundError,
    PolicyBlocked,
    RateLimitError,
    TransportError,
)
from lucnt.ledger import (
    aclose,
    authorize,
    authorize_async,
    capture,
    capture_async,
    check_budget,
    check_budget_async,
    mark_failed_delivery,
    mark_failed_delivery_async,
    query,
    query_async,
    spend,
    spend_async,
    void,
    void_async,
)
from lucnt.meter import meter
from lucnt.models import (
    BudgetStatus,
    BudgetSummary,
    CostPerOutput,
    FinancialMemory,
    JournalLine,
    Money,
    ReconException,
    SpendFingerprint,
    SpendProjection,
    TaskContext,
    TaxInfo,
    Transaction,
    VendorShare,
    Verdict,
    VoidResult,
)

__all__ = [
    "APIError",
    "AuthError",
    "BudgetExhausted",
    "BudgetStatus",
    "BudgetSummary",
    "ConfigurationError",
    "ConflictError",
    "ContractError",
    "CostPerOutput",
    "FinancialMemory",
    "IdempotencyConflict",
    "InvalidRequestError",
    "JournalLine",
    "LucntError",
    "Money",
    "NotFoundError",
    "PolicyBlocked",
    "RateLimitError",
    "ReconException",
    "SpendFingerprint",
    "SpendProjection",
    "TaskContext",
    "TaxInfo",
    "Transaction",
    "TransportError",
    "VendorShare",
    "Verdict",
    "VoidResult",
    "__version__",
    "aclose",
    "authorize",
    "authorize_async",
    "bind_context",
    "capture",
    "capture_async",
    "check_budget",
    "check_budget_async",
    "configure",
    "mark_failed_delivery",
    "mark_failed_delivery_async",
    "meter",
    "operation",
    "query",
    "query_async",
    "spend",
    "spend_async",
    "task",
    "void",
    "void_async",
]
