"""Dashboard auth end-to-end: login, refresh, the discriminator, and scoping.

Four things here can be silently wrong:

* the **auth-context discriminator** — two principal types on one header, where a
  mis-parse is an auth bypass rather than a 401;
* the **cross-owner leak** — an `agent_owner` must not be able to read
  another owner's agent, and the closing evidence is this test, not a hidden nav
  item;
* **role gating** — a page a role cannot see must have an endpoint it cannot call;
* **token type confusion** — a refresh token accepted as a bearer credential
  silently turns a 15-minute window into a 7-day one.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from lucnt_api.database import get_db
from lucnt_api.main import app
from lucnt_api.models import Agent, ApiKey, ApiKeyMode, Company, Entity
from lucnt_api.models.user import User, UserRole
from lucnt_api.security import (
    generate_api_key,
    hash_api_key,
    hash_password,
    mint_refresh_token,
)

PASSWORD = "correct horse battery staple"


@pytest.fixture
def client(db_session: Session) -> Iterator[TestClient]:
    app.dependency_overrides[get_db] = lambda: db_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.pop(get_db, None)


@dataclass
class World:
    """A company with one agent per owner, plus a user for every role."""

    company: Company
    api_key: str
    users: dict[UserRole, User]
    owned_agent: Agent
    other_agent: Agent


@pytest.fixture
def world(db_session: Session) -> World:
    company = Company(name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}")
    db_session.add(company)
    db_session.flush()

    plaintext = generate_api_key(ApiKeyMode.LIVE)
    db_session.add(
        ApiKey(
            company_id=company.id,
            key_prefix=plaintext[:12],
            key_hash=hash_api_key(plaintext),
            name="k",
            mode=ApiKeyMode.LIVE,
        )
    )
    entity = Entity(
        company_id=company.id,
        name="LUCNT IN",
        code="in",
        full_name="LUCNT India Pvt Ltd",
        currency="INR",
        accounting_std="Ind AS",
        tax_regime="IN-GST",
        timezone="Asia/Kolkata",
    )
    db_session.add(entity)
    db_session.flush()

    users: dict[UserRole, User] = {}
    for role in UserRole:
        user = User(
            company_id=company.id,
            email=f"{role.value}-{uuid.uuid4().hex[:6]}@acme.test",
            name=role.value,
            role=role,
            password_hash=hash_password(PASSWORD),
        )
        db_session.add(user)
        users[role] = user
    db_session.flush()

    owned = Agent(
        company_id=company.id,
        entity_id=entity.id,
        name="Mine",
        slug="mine",
        glyph="◑",
        role="Research",
        monthly_budget=Decimal("1000"),
        owner_user_id=users[UserRole.AGENT_OWNER].id,
    )
    other = Agent(
        company_id=company.id,
        entity_id=entity.id,
        name="Theirs",
        slug="theirs",
        glyph="§",
        role="Legal",
        monthly_budget=Decimal("1000"),
        owner_user_id=users[UserRole.CFO].id,
    )
    db_session.add_all([owned, other])
    db_session.flush()
    return World(company, plaintext, users, owned, other)


def _login(client: TestClient, user: User) -> str:
    response = client.post(
        "/auth/login", json={"email": user.email, "password": PASSWORD}
    )
    assert response.status_code == 200, response.text
    return str(response.json()["access_token"])


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------------- #
# Login
# --------------------------------------------------------------------------- #


def test_login_returns_an_access_token_and_sets_the_refresh_cookie(
    client: TestClient, world: World
) -> None:
    response = client.post(
        "/auth/login",
        json={"email": world.users[UserRole.CFO].email, "password": PASSWORD},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] > 0
    cookie = response.cookies.get("lucnt_refresh")
    assert cookie, "the refresh token must be set as a cookie, not returned in the body"
    assert "lucnt_refresh" not in response.text


def test_the_refresh_cookie_is_httponly_and_samesite_strict(
    client: TestClient, world: World
) -> None:
    """The two attributes that make an XSS or a cross-site POST useless."""
    response = client.post(
        "/auth/login",
        json={"email": world.users[UserRole.CFO].email, "password": PASSWORD},
    )
    header = response.headers["set-cookie"].lower()
    assert "httponly" in header
    assert "samesite=strict" in header


def test_a_wrong_password_and_an_unknown_email_are_indistinguishable(
    client: TestClient, world: World
) -> None:
    """Distinguishing them turns the endpoint into an account enumerator."""
    wrong = client.post(
        "/auth/login",
        json={"email": world.users[UserRole.CFO].email, "password": "nope"},
    )
    unknown = client.post(
        "/auth/login", json={"email": "nobody@acme.test", "password": PASSWORD}
    )
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()


def test_a_deactivated_user_cannot_log_in(
    client: TestClient, world: World, db_session: Session
) -> None:
    user = world.users[UserRole.CFO]
    user.is_active = False
    db_session.flush()
    response = client.post(
        "/auth/login", json={"email": user.email, "password": PASSWORD}
    )
    assert response.status_code == 401


# --------------------------------------------------------------------------- #
# The discriminator — one header, two principal types
# --------------------------------------------------------------------------- #


def test_an_api_key_still_authenticates_unchanged(
    client: TestClient, world: World
) -> None:
    """The SDK path must be untouched by the addition of human auth."""
    response = client.get("/agents", headers=_auth(world.api_key))
    assert response.status_code == 200


def test_a_jwt_authenticates_the_same_endpoint(
    client: TestClient, world: World
) -> None:
    token = _login(client, world.users[UserRole.CFO])
    response = client.get("/agents", headers=_auth(token))
    assert response.status_code == 200


def test_a_refresh_token_is_rejected_as_a_bearer_credential(
    client: TestClient, world: World
) -> None:
    """Token-type confusion would stretch a 15-minute window into seven days."""
    user = world.users[UserRole.CFO]
    refresh = mint_refresh_token(user.id, user.company_id)
    response = client.get("/agents", headers=_auth(refresh))
    assert response.status_code == 401


def test_a_garbage_bearer_token_is_a_401_not_a_500(client: TestClient) -> None:
    assert client.get("/agents", headers=_auth("not-a-token")).status_code == 401


def test_a_credential_shaped_like_a_key_but_unknown_is_a_401(
    client: TestClient,
) -> None:
    """The prefix routes it to the key path, where it must simply not resolve."""
    assert client.get("/agents", headers=_auth("lucnt_live_x")).status_code == 401


def test_auth_me_rejects_an_api_key(client: TestClient, world: World) -> None:
    """A valid credential of the wrong *kind*: 400, not 403."""
    response = client.get("/auth/me", headers=_auth(world.api_key))
    assert response.status_code == 400


# --------------------------------------------------------------------------- #
# /auth/me — nav and landing are data
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("role", list(UserRole))
def test_me_returns_nav_and_landing_for_every_role(
    client: TestClient, world: World, role: UserRole
) -> None:
    token = _login(client, world.users[role])
    body = client.get("/auth/me", headers=_auth(token)).json()
    assert body["perms"]["role"] == role.value
    assert body["perms"]["nav"], "the sidebar renders from this; it cannot be empty"
    assert body["perms"]["landing"] in {n["path"] for n in body["perms"]["nav"]}


# --------------------------------------------------------------------------- #
# Refresh
# --------------------------------------------------------------------------- #


def test_refresh_mints_a_new_access_token_from_the_cookie(
    client: TestClient, world: World
) -> None:
    _login(client, world.users[UserRole.CFO])  # sets the cookie on the client
    response = client.post("/auth/refresh")
    assert response.status_code == 200
    fresh = client.get("/agents", headers=_auth(response.json()["access_token"]))
    assert fresh.status_code == 200


def test_refresh_without_a_cookie_is_a_401(client: TestClient) -> None:
    assert client.post("/auth/refresh").status_code == 401


def test_an_access_token_is_rejected_by_refresh(
    client: TestClient, world: World
) -> None:
    """The inverse type confusion: an access token must not extend a session."""
    access = _login(client, world.users[UserRole.CFO])
    client.cookies.set("lucnt_refresh", access)
    assert client.post("/auth/refresh").status_code == 401


def test_logout_clears_the_cookie(client: TestClient, world: World) -> None:
    _login(client, world.users[UserRole.CFO])
    assert client.post("/auth/logout").status_code == 204
    assert not client.cookies.get("lucnt_refresh")


# --------------------------------------------------------------------------- #
# The cross-owner leak test
# --------------------------------------------------------------------------- #


def test_an_agent_owner_cannot_list_another_owners_agent(
    client: TestClient, world: World
) -> None:
    """**Closes the cross-owner leak.** Scoping is in the SQL, not in the sidebar."""
    token = _login(client, world.users[UserRole.AGENT_OWNER])
    body = client.get("/agents", headers=_auth(token)).json()
    slugs = {item["slug"] for item in body["items"]}
    assert slugs == {"mine"}
    # `total` must reflect the scoped query too: a total of 2 over one visible
    # row is the same leak, told as a number.
    assert body["total"] == 1


def test_an_agent_owner_cannot_fetch_another_owners_agent_by_id(
    client: TestClient, world: World
) -> None:
    """404 rather than 403 — confirming the id exists is itself a disclosure."""
    token = _login(client, world.users[UserRole.AGENT_OWNER])
    response = client.get(f"/agents/{world.other_agent.id}", headers=_auth(token))
    assert response.status_code == 404


def test_a_cfo_sees_both_agents(client: TestClient, world: World) -> None:
    """The control: scoping must narrow the owner, not everyone."""
    token = _login(client, world.users[UserRole.CFO])
    body = client.get("/agents", headers=_auth(token)).json()
    assert {item["slug"] for item in body["items"]} == {"mine", "theirs"}


def test_an_agent_owners_budgets_are_scoped_too(
    client: TestClient, world: World
) -> None:
    token = _login(client, world.users[UserRole.AGENT_OWNER])
    body = client.get("/budgets", headers=_auth(token)).json()
    assert len(body["agents"]) == 1
    # Rollups sum across a whole workflow, so a scoped principal gets none.
    assert body["workflows"] == []


# --------------------------------------------------------------------------- #
# Route gating — a page you cannot see is an endpoint you cannot call
# --------------------------------------------------------------------------- #


def test_an_engineer_cannot_read_the_books(client: TestClient, world: World) -> None:
    token = _login(client, world.users[UserRole.ENGINEER])
    assert client.get("/books/tb", headers=_auth(token)).status_code == 403


def test_an_auditor_can_read_the_books(client: TestClient, world: World) -> None:
    token = _login(client, world.users[UserRole.AUDITOR])
    assert client.get("/books/tb", headers=_auth(token)).status_code == 200


def test_an_agent_owner_cannot_reach_the_approval_queue(
    client: TestClient, world: World
) -> None:
    token = _login(client, world.users[UserRole.AGENT_OWNER])
    assert client.get("/approvals", headers=_auth(token)).status_code == 403


def test_an_auditor_cannot_write_a_budget(client: TestClient, world: World) -> None:
    """The §10.3 checklist item, exercised rather than asserted in prose."""
    token = _login(client, world.users[UserRole.AUDITOR])
    response = client.patch(
        f"/budgets/{world.owned_agent.id}",
        headers=_auth(token),
        json={"monthly_budget": "2000"},
    )
    assert response.status_code == 403


def test_an_engineer_cannot_write_a_budget(client: TestClient, world: World) -> None:
    token = _login(client, world.users[UserRole.ENGINEER])
    response = client.patch(
        f"/budgets/{world.owned_agent.id}",
        headers=_auth(token),
        json={"monthly_budget": "2000"},
    )
    assert response.status_code == 403


def test_a_cfo_can_write_a_budget(client: TestClient, world: World) -> None:
    token = _login(client, world.users[UserRole.CFO])
    response = client.patch(
        f"/budgets/{world.owned_agent.id}",
        headers=_auth(token),
        json={"monthly_budget": "2000"},
    )
    assert response.status_code == 200


def test_a_budget_write_by_a_human_is_audited_with_their_user_id(
    client: TestClient, world: World
) -> None:
    """A governance change with a NULL actor is not a governance record."""
    token = _login(client, world.users[UserRole.CFO])
    client.patch(
        f"/budgets/{world.owned_agent.id}",
        headers=_auth(token),
        json={"monthly_budget": "2000"},
    )
    logs = client.get("/audit-logs", headers=_auth(token)).json()["items"]
    entry = next(e for e in logs if e["action"].startswith("budget"))
    assert entry["actor_user_id"] == str(world.users[UserRole.CFO].id)
    assert entry["actor_api_key_id"] is None


def test_a_human_cannot_move_money(client: TestClient, world: World) -> None:
    """`require_live` refuses a dashboard session outright."""
    token = _login(client, world.users[UserRole.CFO])
    response = client.post(
        "/transactions/authorize",
        headers=_auth(token),
        json={
            "agent": "mine",
            "entity": "in",
            "vendor": "openai",
            "amount": "1.00",
            "currency": "INR",
            "reason": "test",
        },
    )
    assert response.status_code == 403


# --------------------------------------------------------------------------- #
# The new read endpoints
# --------------------------------------------------------------------------- #


def test_api_keys_are_masked(client: TestClient, world: World) -> None:
    """Only a 12-char prefix is stored, so the secret cannot leak by accident."""
    token = _login(client, world.users[UserRole.ENGINEER])
    body = client.get("/api-keys", headers=_auth(token)).json()
    assert body["total"] == 1
    row = body["items"][0]
    assert row["key_prefix"] == world.api_key[:12]
    assert world.api_key not in client.get("/api-keys", headers=_auth(token)).text


def test_api_keys_are_gated_on_the_developers_nav(
    client: TestClient, world: World
) -> None:
    token = _login(client, world.users[UserRole.AUDITOR])
    assert client.get("/api-keys", headers=_auth(token)).status_code == 403


def test_entities_are_readable_by_every_role(client: TestClient, world: World) -> None:
    """The workspace switcher is in the top bar of every page."""
    for role in UserRole:
        token = _login(client, world.users[role])
        response = client.get("/entities", headers=_auth(token))
        assert response.status_code == 200, role
        assert response.json()["total"] == 1


def test_users_exposes_no_password_hash(client: TestClient, world: World) -> None:
    token = _login(client, world.users[UserRole.CFO])
    response = client.get("/users", headers=_auth(token))
    assert response.status_code == 200
    assert "password" not in response.text.lower()


def test_authorizations_filters_by_a_single_status(
    client: TestClient, world: World
) -> None:
    token = _login(client, world.users[UserRole.CFO])
    response = client.get("/authorizations?status=blocked", headers=_auth(token))
    assert response.status_code == 200
    assert response.json()["items"] == []


def test_authorizations_rejects_an_unknown_status(
    client: TestClient, world: World
) -> None:
    token = _login(client, world.users[UserRole.CFO])
    response = client.get("/authorizations?status=nonsense", headers=_auth(token))
    assert response.status_code == 422
