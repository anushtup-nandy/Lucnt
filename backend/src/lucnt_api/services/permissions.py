"""The one RBAC seam: `resolve_perms(user) -> Perms` (frontend/PLAN.md §10.3).

`CLAUDE.local.md` "presets now, custom later": `users.role` is a typed enum today
and permissions resolve through this single function, so swapping in
table-backed RBAC later is a change to one module rather than to every router.

**No scattered `if role == ...` checks anywhere else.** If you find yourself
writing one, the answer belongs in `_ROLE_TABLE` below.

Two mechanics are non-negotiable (`CLAUDE.local.md` §Step 4):

1. **Nav is data.** The frontend renders `perms.nav`; it never hardcodes a
   sidebar. The same tuple is the server-side route gate (`require_nav`), so the
   thing that hides a page is the thing that denies its endpoint — they cannot
   drift apart.
2. **Scope is enforced in the query.** `perms.agent_scope == "owned"` filters
   `agents.owner_user_id` in SQL (`scope_agents`). Hiding a nav item without
   scoping the query is a security hole, not a UI decision — F-5 closes on the
   cross-owner leak test, not on a hidden link.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import Select

from lucnt_api.models import Agent
from lucnt_api.models.user import User, UserRole

__all__ = ["NavItem", "NavKey", "Perms", "resolve_perms", "scope_agents", "NAV_LABELS"]

# One key per dashboard domain. These are the strings the frontend routes on and
# the strings `require_nav` gates on — the single vocabulary shared by both.
NavKey = Literal[
    "overview",
    "agents",
    "transactions",
    "books",
    "budgets",
    "policies",
    "approvals",
    "recon",
    "insights",
    "memory",
    "developers",
    "settings",
]

NAV_LABELS: dict[str, tuple[str, str]] = {
    "overview": ("Overview", "/overview"),
    "agents": ("Agents", "/agents"),
    "transactions": ("Transactions", "/transactions"),
    "books": ("Books", "/books"),
    "budgets": ("Budgets", "/budgets"),
    "policies": ("Policies", "/policies"),
    "approvals": ("Approvals", "/approvals"),
    "recon": ("Reconciliation", "/recon"),
    "insights": ("Insights", "/insights"),
    "memory": ("Memory", "/memory"),
    "developers": ("Developers", "/developers"),
    "settings": ("Settings", "/settings"),
}


@dataclass(frozen=True)
class NavItem:
    """One sidebar entry. `key` is also the server-side route-gate identifier."""

    key: str
    label: str
    path: str


@dataclass(frozen=True)
class Perms:
    """Everything a principal may see and do, resolved once per request."""

    role: UserRole
    nav: tuple[NavItem, ...]
    landing: str
    # Enforced in the QUERY (`scope_agents`), never only in the UI.
    agent_scope: Literal["all", "owned"]
    can_write_budgets: bool
    can_write_policies: bool
    can_approve: bool
    can_read_books: bool

    def allows(self, key: str) -> bool:
        """True if `key` is one of this principal's nav domains."""
        return any(item.key == key for item in self.nav)


# role -> (nav keys in display order, landing path, agent scope, write flags)
#
# Personas per `CLAUDE.local.md` §Step 4. `cfo` is the mockup's only persona
# ("Arvind Sharma, Finance, admin") and therefore the only one that sees all 12.
_ROLE_TABLE: dict[UserRole, dict[str, Any]] = {
    UserRole.ENGINEER: {
        "nav": (
            "developers",
            "overview",
            "agents",
            "transactions",
            "policies",
            "settings",
        ),
        "landing": "/developers",
        "agent_scope": "all",
        "can_write_budgets": False,
        "can_write_policies": True,
        "can_approve": False,
    },
    UserRole.CFO: {
        "nav": tuple(NAV_LABELS),
        "landing": "/overview",
        "agent_scope": "all",
        "can_write_budgets": True,
        "can_write_policies": True,
        "can_approve": True,
    },
    UserRole.AGENT_OWNER: {
        "nav": ("agents", "overview", "transactions", "budgets", "memory"),
        "landing": "/agents",
        "agent_scope": "owned",
        "can_write_budgets": True,
        "can_write_policies": False,
        "can_approve": False,
    },
    UserRole.APPROVER: {
        "nav": ("approvals", "overview", "agents", "transactions"),
        "landing": "/approvals",
        "agent_scope": "all",
        "can_write_budgets": False,
        "can_write_policies": False,
        "can_approve": True,
    },
    UserRole.AUDITOR: {
        # Read-only: every write path 403s for this role, server-side.
        "nav": ("books", "overview", "agents", "transactions", "recon", "settings"),
        "landing": "/books",
        "agent_scope": "all",
        "can_write_budgets": False,
        "can_write_policies": False,
        "can_approve": False,
    },
}


def resolve_perms(user: User) -> Perms:
    """The single source of truth for what a user may see and do.

    Args:
        user: The authenticated human principal.

    Returns:
        A frozen `Perms` for this request. `can_read_books` is *derived* from nav
        membership rather than stated twice, so a role that gains the Books page
        cannot end up with the page and not the data (or the reverse).
    """
    spec = _ROLE_TABLE[user.role]
    nav = tuple(
        NavItem(key=key, label=NAV_LABELS[key][0], path=NAV_LABELS[key][1])
        for key in spec["nav"]
    )
    return Perms(
        role=user.role,
        nav=nav,
        landing=spec["landing"],
        agent_scope=spec["agent_scope"],
        can_write_budgets=spec["can_write_budgets"],
        can_write_policies=spec["can_write_policies"],
        can_approve=spec["can_approve"],
        can_read_books=any(item.key == "books" for item in nav),
    )


def scope_agents(
    stmt: Select[Any], perms: Perms | None, user_id: uuid.UUID | None
) -> Select[Any]:
    """Narrow a statement selecting `Agent` to the rows this principal may see.

    A no-op for API-key principals (`perms is None`) and for `agent_scope="all"`.
    For `agent_owner` it adds `agents.owner_user_id = :user_id` **to the SQL** —
    which is the whole point: filtering after the fact would still have read the
    other owner's rows out of the database, and would break pagination totals.

    Args:
        stmt: A `select(Agent)`-rooted statement, or one already joined to `Agent`.
        perms: Resolved perms, or `None` for an API-key principal.
        user_id: The human principal's id, or `None`.

    Returns:
        The statement, narrowed where required.
    """
    if perms is None or perms.agent_scope == "all" or user_id is None:
        return stmt
    return stmt.where(Agent.owner_user_id == user_id)
