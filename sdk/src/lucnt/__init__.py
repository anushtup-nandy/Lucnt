"""lucnt: financial infrastructure that attaches to your existing agents.

Phase 1 (A) public surface: `configure()`, `task()`, `authorize()`,
`capture()`, `spend()` — the universal spend-type-agnostic primitive — plus
their typed models and the SDK's exception hierarchy. `meter`/`query`/
`check_budget` land in later phases as the backend surfaces they depend on
are built out (see `sdk/PLAN.md` §5).
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
from lucnt.ledger import authorize, capture, spend
from lucnt.models import JournalLine, TaskContext, TaxInfo, Transaction, Verdict

__all__ = [
    "AuthError",
    "BudgetExhausted",
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
    "configure",
    "spend",
    "task",
]
