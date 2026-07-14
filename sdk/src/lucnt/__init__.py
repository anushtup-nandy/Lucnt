"""lucnt: financial infrastructure that attaches to your existing agents.

Public surface: `configure()`, `task()`, `authorize()`, `capture()`, `spend()`
— the universal spend-type-agnostic primitive — plus `check_budget()` (Phase 3
self-regulation read), `meter()` (Phase 3.5 auto-instrumentation for LLM/vendor
clients), `mark_failed_delivery()` (Phase 4 recovery hook), and `query()` (Phase 5
living financial memory), their typed models, and the SDK's exception hierarchy.
"""

from __future__ import annotations

from lucnt._version import __version__
from lucnt.config import configure
from lucnt.context import task
from lucnt.exceptions import (
    AuthError,
    BudgetExhausted,
    ConfigurationError,
    LucntError,
    PolicyBlocked,
    TransportError,
)
from lucnt.ledger import (
    authorize,
    capture,
    check_budget,
    mark_failed_delivery,
    query,
    spend,
)
from lucnt.meter import meter
from lucnt.models import (
    BudgetStatus,
    BudgetSummary,
    CostPerOutput,
    FinancialMemory,
    JournalLine,
    ReconException,
    SpendFingerprint,
    SpendProjection,
    TaskContext,
    TaxInfo,
    Transaction,
    VendorShare,
    Verdict,
)

__all__ = [
    "AuthError",
    "BudgetExhausted",
    "BudgetStatus",
    "BudgetSummary",
    "ConfigurationError",
    "CostPerOutput",
    "FinancialMemory",
    "JournalLine",
    "LucntError",
    "PolicyBlocked",
    "ReconException",
    "SpendFingerprint",
    "SpendProjection",
    "TaskContext",
    "TaxInfo",
    "Transaction",
    "TransportError",
    "VendorShare",
    "Verdict",
    "__version__",
    "authorize",
    "capture",
    "check_budget",
    "configure",
    "mark_failed_delivery",
    "meter",
    "query",
    "spend",
    "task",
]
