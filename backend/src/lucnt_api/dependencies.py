"""FastAPI dependencies: DB session, auth, scope/role gating, throttling.

**One header, two principal types (P7.0).** `Authorization: Bearer <credential>`
carries either an SDK API key (`lucnt_live_…` / `lucnt_test_…`) or a dashboard
access JWT. `get_auth_context` discriminates on the prefix, so all 42 existing
endpoints gained human auth without changing a single signature — the change
lands here. `AuthContext` holds **exactly one** of `api_key` / `user`.

Gates, layered on that one resolution:

* ``get_current_company`` — any authenticated principal; read endpoints.
* ``require_nav(key)`` — the human route gate. A user may only reach a domain
  present in `perms.nav`. Same tuple the sidebar renders from, so a hidden page
  and a denied endpoint can never drift apart. No-op for API keys.
* ``require_write(flag)`` — a governance mutation. API key: live + admin scope,
  exactly as before. User: the named `Perms` flag must be true.
* ``require_live`` — a live API key; the money path (authorize/capture/reverse).
  **Always 403 for a human principal**: a dashboard session must not be able to
  move money, only to govern it.
* ``require_admin`` — governance mutations with no finer-grained flag. API key:
  live + admin. User: any role except `auditor`, which is read-only by
  definition (`frontend/PLAN.md` §10.3).
* ``require_cheap_enough`` — the tight `expensive` bucket, layered *on top of*
  another gate, never instead of one.

Rate limiting keys on `api_key.id` or `user.id` — never on a secret, which must
not reach Redis.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from lucnt_api.database import get_db
from lucnt_api.models import ApiKey, ApiKeyMode, ApiKeyScope, Company
from lucnt_api.models.user import User, UserRole
from lucnt_api.rate_limit import Bucket, client_ip, get_limiter
from lucnt_api.security import (
    InvalidTokenError,
    decode_token,
    hash_api_key,
    looks_like_api_key,
)
from lucnt_api.services.permissions import Perms, resolve_perms

__all__ = [
    "get_db",
    "get_auth_context",
    "get_current_company",
    "require_live",
    "require_admin",
    "require_cheap_enough",
    "require_nav",
    "require_write",
    "AuthContext",
    "WriteFlag",
]

_bearer_scheme = HTTPBearer(auto_error=False)

# Only rewrite last_used_at when the stored value is older than this, so the hot
# api_keys row isn't UPDATE'd (and WAL-written, row-locked) on every request (T0-17).
_LAST_USED_THROTTLE = timedelta(minutes=5)

# The `Perms` booleans a route may demand. A `Literal` rather than a bare `str`
# so a typo is a mypy error, not a permission that is silently never satisfied.
WriteFlag = Literal["can_write_budgets", "can_write_policies", "can_approve"]


@dataclass(frozen=True)
class AuthContext:
    """The authenticated principal. Exactly one of `api_key` / `user` is set.

    `perms` is populated for human principals only; `None` means "an API key,
    which is governed by mode/scope rather than by role".
    """

    company: Company
    api_key: ApiKey | None = None
    user: User | None = None
    perms: Perms | None = None

    @property
    def actor_label(self) -> str:
        """A short identifier for logs and audit rows."""
        if self.user is not None:
            return f"user:{self.user.email}"
        return f"key:{self.api_key.key_prefix}" if self.api_key else "unknown"


def _company_or_500(db: Session, company_id: object) -> Company:
    company = db.get(Company, company_id)
    if company is None:  # FK-guaranteed; explicit guard, not an assert (T0-18)
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "credential references a missing company",
        )
    return company


def _resolve_api_key(request: Request, token: str, db: Session) -> AuthContext:
    """The SDK path, unchanged from Phase 1 apart from being factored out."""
    limiter = get_limiter()
    ip = client_ip(request)
    # Checked *before* the lookup: the cost of a brute-force guess is this DB
    # round trip, so limiting after it would still pay for every attempt (T3-7).
    limiter.guard_auth_attempt(ip)

    key_hash = hash_api_key(token)
    api_key = db.query(ApiKey).filter(ApiKey.key_hash == key_hash).one_or_none()
    if api_key is None or not api_key.is_active:
        limiter.record_auth_failure(ip)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid API key")

    # Only now that the principal is known can the per-key bucket be charged —
    # keyed by the key's id, never the secret, which must not reach Redis.
    limiter.check(Bucket.DEFAULT, str(api_key.id))

    # Throttled bookkeeping (T0-17): avoid a commit on the hot key row per request.
    now = datetime.now(UTC)
    if api_key.last_used_at is None or now - api_key.last_used_at > _LAST_USED_THROTTLE:
        api_key.last_used_at = now
        db.commit()

    return AuthContext(company=_company_or_500(db, api_key.company_id), api_key=api_key)


def _resolve_user(request: Request, token: str, db: Session) -> AuthContext:
    """The dashboard path: verify an access JWT and resolve the human principal."""
    limiter = get_limiter()
    ip = client_ip(request)
    limiter.guard_auth_attempt(ip)

    try:
        claims = decode_token(token, "access")
    except InvalidTokenError:
        limiter.record_auth_failure(ip)
        # 401, deliberately: the SPA's interceptor treats 401 as "try one silent
        # refresh", which is exactly right for an expired access token.
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "Invalid or expired session"
        ) from None

    user = db.get(User, claims.user_id)
    # `company_id` is checked against the token's claim as well as the row: a
    # user moved between companies must not keep a token that names the old one.
    if user is None or not user.is_active or user.company_id != claims.company_id:
        limiter.record_auth_failure(ip)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired session")

    limiter.check(Bucket.DEFAULT, f"user:{user.id}")
    return AuthContext(
        company=_company_or_500(db, user.company_id),
        user=user,
        perms=resolve_perms(user),
    )


def get_auth_context(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    db: Session = Depends(get_db),
) -> AuthContext:
    """Resolve the `Authorization: Bearer <credential>` header to a principal.

    Raises:
        HTTPException: 401 if the header is missing, or the credential is
            unknown, inactive or expired. The SDK's `Transport` maps 401/403 to
            `AuthError`. 429 if this IP has exhausted its failed-attempt budget,
            or the resolved principal has exhausted its request budget (T3-7).
    """
    if credentials is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing credentials")
    token = credentials.credentials
    if looks_like_api_key(token):
        return _resolve_api_key(request, token, db)
    return _resolve_user(request, token, db)


def get_current_company(
    ctx: AuthContext = Depends(get_auth_context),
) -> Company:
    """Any authenticated principal — read endpoints."""
    return ctx.company


def require_live(ctx: AuthContext = Depends(get_auth_context)) -> Company:
    """A live API key — the money path. Test keys cannot write the live ledger (T0-13).

    A human principal is refused outright. `authorize`/`capture` are what an
    agent calls at the moment of spend; a dashboard session posting one would be
    money moving with no agent behind it, which is the opposite of the product.
    """
    if ctx.user is not None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "the money path is SDK-only; a dashboard session cannot move money",
        )
    if ctx.api_key is None or ctx.api_key.mode != ApiKeyMode.LIVE:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "a live API key is required to write to the ledger",
        )
    return ctx.company


def require_cheap_enough(ctx: AuthContext = Depends(get_auth_context)) -> Company:
    """The tight `expensive` bucket, for endpoints that walk the ledger (T3-7).

    Layered *on top of* the per-principal default bucket rather than replacing
    it, and lives here rather than in `rate_limit.py` so it can key on the
    resolved principal id — keying on the raw bearer token would write
    credential material into Redis. Apply to `/recon/scan` and
    `/intelligence/recompute`, whose cost is proportional to ledger size (T3-2).
    """
    identity = (
        f"user:{ctx.user.id}" if ctx.user is not None else str(ctx.api_key.id)  # type: ignore[union-attr]
    )
    get_limiter().check(Bucket.EXPENSIVE, identity)
    return ctx.company


def _require_key_admin(ctx: AuthContext) -> None:
    """The API-key half of every governance gate (T0-12), unchanged."""
    if ctx.api_key is None or ctx.api_key.mode != ApiKeyMode.LIVE:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "a live API key is required for this operation",
        )
    if ctx.api_key.scope != ApiKeyScope.ADMIN:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "an admin-scoped API key is required for this operation",
        )


def require_admin(ctx: AuthContext = Depends(get_auth_context)) -> Company:
    """Governance mutation with no finer-grained flag (T0-12).

    API key: live + admin scope. User: anything but `auditor`, whose whole
    definition is read-only — the `frontend/PLAN.md` §10.3 checklist item
    "every write path 403s for `auditor` server-side" is enforced here and in
    `require_write`, so no write path can miss it.
    """
    if ctx.user is not None:
        if ctx.user.role == UserRole.AUDITOR:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, "the auditor role is read-only"
            )
        return ctx.company
    _require_key_admin(ctx)
    return ctx.company


def require_write(flag: WriteFlag) -> Callable[..., Company]:
    """Build a dependency demanding one named `Perms` write flag.

    API-key principals are gated exactly as `require_admin` gates them — the
    flags describe *human* roles, and an admin key has always been able to do
    all of this. Human principals must hold the flag.

    Args:
        flag: The `Perms` attribute that must be true, e.g. `can_write_budgets`.

    Returns:
        A dependency callable for `Depends(...)`.
    """

    def dependency(ctx: AuthContext = Depends(get_auth_context)) -> Company:
        if ctx.perms is not None:
            if not getattr(ctx.perms, flag):
                raise HTTPException(
                    status.HTTP_403_FORBIDDEN,
                    f"your role ({ctx.perms.role.value}) cannot perform this action",
                )
            return ctx.company
        _require_key_admin(ctx)
        return ctx.company

    return dependency


def require_nav(key: str) -> Callable[..., AuthContext]:
    """Build a dependency gating a whole domain on nav membership.

    This is the server-side twin of the sidebar: `perms.nav` is what the
    frontend renders *and* what this checks, so a role that cannot see a page
    also cannot call its endpoints. Hiding a nav item without denying the
    endpoint is the security hole `CLAUDE.local.md` §Step 4 names explicitly.

    No-op for API-key principals: the SDK's access is governed by key mode and
    scope, and narrowing it here would break existing integrations.

    Args:
        key: A `NavKey`, e.g. `"books"`.

    Returns:
        A dependency callable yielding the `AuthContext`, so a route can gate
        and read the principal in one dependency.
    """

    def dependency(ctx: AuthContext = Depends(get_auth_context)) -> AuthContext:
        if ctx.perms is not None and not ctx.perms.allows(key):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"your role ({ctx.perms.role.value}) cannot access {key}",
            )
        return ctx

    return dependency
