"""`resolve_perms` per role — the whole persona model rests on this one function.

Every nav item, every landing page and every write gate is derived from it. A wrong
row in `_ROLE_TABLE` is a page a role should not see, or a write it should not
be able to make.
"""

from __future__ import annotations

import pytest

from lucnt_api.models.user import User, UserRole
from lucnt_api.services.permissions import NAV_LABELS, resolve_perms


def _user(role: UserRole) -> User:
    return User(email="x@y.z", name="X", role=role, password_hash="x")


@pytest.mark.parametrize("role", list(UserRole))
def test_every_role_resolves(role: UserRole) -> None:
    """No role may be missing from the table — a KeyError here is a 500 on login."""
    perms = resolve_perms(_user(role))
    assert perms.role is role
    assert perms.nav, "a role with no nav has nowhere to land"


@pytest.mark.parametrize("role", list(UserRole))
def test_the_landing_page_is_one_the_role_can_actually_reach(role: UserRole) -> None:
    """Landing on a page the nav gate then 403s would be a broken login."""
    perms = resolve_perms(_user(role))
    assert perms.landing in {item.path for item in perms.nav}


@pytest.mark.parametrize("role", list(UserRole))
def test_nav_keys_are_known(role: UserRole) -> None:
    """A typo'd nav key would render a dead sidebar link and gate nothing."""
    perms = resolve_perms(_user(role))
    assert {item.key for item in perms.nav} <= set(NAV_LABELS)


def test_the_cfo_sees_everything() -> None:
    """The mockup's only persona; the one role for which nothing is hidden."""
    perms = resolve_perms(_user(UserRole.CFO))
    assert {item.key for item in perms.nav} == set(NAV_LABELS)
    assert perms.agent_scope == "all"


def test_the_auditor_can_write_nothing() -> None:
    """Every write path 403s for auditor, server-side."""
    perms = resolve_perms(_user(UserRole.AUDITOR))
    assert not perms.can_write_budgets
    assert not perms.can_write_policies
    assert not perms.can_approve
    assert perms.can_read_books


def test_only_the_agent_owner_is_scoped() -> None:
    for role in UserRole:
        perms = resolve_perms(_user(role))
        expected = "owned" if role is UserRole.AGENT_OWNER else "all"
        assert perms.agent_scope == expected, role


def test_can_read_books_agrees_with_the_nav() -> None:
    """Derived, not stated twice — the page and the data cannot disagree."""
    for role in UserRole:
        perms = resolve_perms(_user(role))
        assert perms.can_read_books == perms.allows("books"), role


def test_the_approver_lands_on_the_queue_it_exists_for() -> None:
    perms = resolve_perms(_user(UserRole.APPROVER))
    assert perms.landing == "/approvals"
    assert perms.can_approve


def test_the_engineer_lands_on_developers_and_cannot_move_budgets() -> None:
    perms = resolve_perms(_user(UserRole.ENGINEER))
    assert perms.landing == "/developers"
    assert perms.can_write_policies
    assert not perms.can_write_budgets
    assert not perms.allows("books")
