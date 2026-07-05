"""lucnt: financial infrastructure that attaches to your existing agents.

Public surface: `configure()`, `task()`, `authorize()`, `capture()`, `spend()`
— the universal spend-type-agnostic primitive — plus `check_budget()` (Phase 3
self-regulation read), their typed models, and the SDK's exception hierarchy.
`meter`/`query` land in later phases as the backend surfaces they depend on are
built out (see `sdk/PLAN.md` §5).
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
    "spend",
    "task",
]
