"""lucnt: financial infrastructure that attaches to your existing agents.

Public surface: `configure()`, `task()`, `authorize()`, `capture()`, `spend()`
— the universal spend-type-agnostic primitive — plus `check_budget()` (Phase 3
self-regulation read) and `meter()` (Phase 3.5 auto-instrumentation for
LLM/vendor clients), their typed models, and the SDK's exception hierarchy.
`query` lands in a later phase (see `sdk/PLAN.md` §5).
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
from lucnt.ledger import authorize, capture, check_budget, spend
from lucnt.meter import meter
from lucnt.models import (
    BudgetStatus,
    JournalLine,
    TaskContext,
    TaxInfo,
    Transaction,
    Verdict,
)

__all__ = [
    "AuthError",
    "BudgetExhausted",
    "BudgetStatus",
    "ConfigurationError",
    "JournalLine",
    "LucntError",
    "PolicyBlocked",
    "TaskContext",
    "TaxInfo",
    "Transaction",
    "TransportError",
    "Verdict",
    "__version__",
    "authorize",
    "capture",
    "check_budget",
    "configure",
    "meter",
    "spend",
    "task",
]
