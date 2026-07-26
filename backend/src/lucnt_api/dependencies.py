"""FastAPI dependencies: DB session, API-key auth, scope/mode gating, throttling.

Four gates layer on one resolution (`get_auth_context`), which itself charges the
caller's per-key rate-limit bucket after resolving the principal (T3-7):

* ``get_current_company`` — any active key; read endpoints.
* ``require_live`` — a live key; the money path (authorize/capture). A test key
  must not drive the live ledger (T0-13); a sandbox data partition is future.
* ``require_admin`` — a live, admin-scoped key; governance mutations (budgets,
  policies, reconciliation, approvals). Separates the governed principal (a
  runtime agent key) from the governor (T0-12).
* ``require_cheap_enough`` — the tight `expensive` bucket for endpoints whose
  cost scales with ledger size. Layered *on top of* another gate, never instead
  of one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from lucnt_api.database import get_db
from lucnt_api.models import ApiKey, ApiKeyMode, ApiKeyScope, Company
from lucnt_api.rate_limit import Bucket, client_ip, get_limiter
from lucnt_api.security import hash_api_key

__all__ = [
    "get_db",
    "get_auth_context",
    "get_current_company",
    "require_live",
    "require_admin",
    "require_cheap_enough",
    "AuthContext",
]

_bearer_scheme = HTTPBearer(auto_error=False)

# Only rewrite last_used_at when the stored value is older than this, so the hot
# api_keys row isn't UPDATE'd (and WAL-written, row-locked) on every request (T0-17).
_LAST_USED_THROTTLE = timedelta(minutes=5)


@dataclass(frozen=True)
class AuthContext:
    """The authenticated principal: the resolved company and the exact key used."""

    company: Company
    api_key: ApiKey


def get_auth_context(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    db: Session = Depends(get_db),
) -> AuthContext:
    """Resolve the `Authorization: Bearer <api_key>` header to a principal.

    Raises:
        HTTPException: 401 if the header is missing, or the key is unknown or
            inactive. The SDK's `Transport` maps 401/403 to `AuthError`.
            429 if this IP has exhausted its failed-attempt budget, or the
            resolved key has exhausted its request budget (T3-7).
    """
    if credentials is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing API key")

    limiter = get_limiter()
    ip = client_ip(request)
    # Checked *before* the lookup: the cost of a brute-force guess is this DB
    # round trip, so limiting after it would still pay for every attempt (T3-7).
    limiter.guard_auth_attempt(ip)

    key_hash = hash_api_key(credentials.credentials)
    api_key = db.query(ApiKey).filter(ApiKey.key_hash == key_hash).one_or_none()
    if api_key is None or not api_key.is_active:
        limiter.record_auth_failure(ip)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid API key")

    # Only now that the principal is known can the per-key bucket be charged —
    # keyed by the key's id, never the secret, which must not reach Redis.
    limiter.check(Bucket.DEFAULT, str(api_key.id))

    # Throttled bookkeeping (T0-17): avoid a commit on the hot key row per request.
    now = datetime.now(timezone.utc)
    if api_key.last_used_at is None or now - api_key.last_used_at > _LAST_USED_THROTTLE:
        api_key.last_used_at = now
        db.commit()

    company = db.get(Company, api_key.company_id)
    if company is None:  # FK-guaranteed; explicit guard, not an assert (T0-18)
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "api key references a missing company",
        )
    return AuthContext(company=company, api_key=api_key)


def get_current_company(
    ctx: AuthContext = Depends(get_auth_context),
) -> Company:
    """Any active key — read endpoints."""
    return ctx.company


def require_live(ctx: AuthContext = Depends(get_auth_context)) -> Company:
    """A live key — the money path. Test keys cannot write the live ledger (T0-13)."""
    if ctx.api_key.mode != ApiKeyMode.LIVE:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "a live API key is required to write to the ledger",
        )
    return ctx.company


def require_cheap_enough(ctx: AuthContext = Depends(get_auth_context)) -> Company:
    """The tight `expensive` bucket, for endpoints that walk the ledger (T3-7).

    Layered *on top of* the per-key default bucket rather than replacing it, and
    lives here rather than in `rate_limit.py` so it can key on the resolved
    `api_key.id` — keying on the raw bearer token would write key material into
    Redis. Apply to `/recon/scan` and `/intelligence/recompute`, whose cost is
    proportional to ledger size (T3-2).
    """
    get_limiter().check(Bucket.EXPENSIVE, str(ctx.api_key.id))
    return ctx.company


def require_admin(ctx: AuthContext = Depends(get_auth_context)) -> Company:
    """A live, admin-scoped key — governance mutations (T0-12)."""
    if ctx.api_key.mode != ApiKeyMode.LIVE:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "a live API key is required for this operation",
        )
    if ctx.api_key.scope != ApiKeyScope.ADMIN:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "an admin-scoped API key is required for this operation",
        )
    return ctx.company
