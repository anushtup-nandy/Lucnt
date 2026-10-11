"""The one RBAC seam: `resolve_perms(user) -> Perms`. No `if role ==` elsewhere.

Nav is data and doubles as the route gate; agent scope is enforced in SQL.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import Select

from lucnt_api.models import Agent
from lucnt_api.models.user import User, UserRole

__all__ = ["NavItem", "NavKey", "Perms", "resolve_perms", "scope_agents", "NAV_LABELS"]

# Shared vocabulary: frontend routes and `require_nav` gates both use these.
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


# role -> nav keys (display order), landing path, agent scope, write flags.
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

    `can_read_books` derives from nav so page and data access can't diverge.
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
    """Narrow an `Agent` select to rows this principal may see, in SQL.

    Filtering in SQL (not after) avoids reading others' rows and keeps
    pagination totals right. No-op for API keys (`perms is None`) and "all" scope.
    """
    if perms is None or perms.agent_scope == "all" or user_id is None:
        return stmt
    return stmt.where(Agent.owner_user_id == user_id)
