"""lucnt: financial infrastructure that attaches to your existing agents.

Core primitive: `authorize()` -> `capture()` (or `spend()`); every call has an
`_async` twin. `meter()` auto-instruments LLM/vendor clients.
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
from lucnt.governor import metered_async_http_client, metered_http_client
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
from lucnt.pricing import refresh_pricing, refresh_pricing_async

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
    "metered_async_http_client",
    "metered_http_client",
    "operation",
    "query",
    "query_async",
    "refresh_pricing",
    "refresh_pricing_async",
    "spend",
    "spend_async",
    "task",
    "void",
    "void_async",
]
