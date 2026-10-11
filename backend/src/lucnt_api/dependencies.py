"""FastAPI dependencies: DB session, auth, scope/role gating, throttling.

The Bearer header carries either an API key or a dashboard JWT, discriminated
by prefix. Rate limits key on principal ids, never secrets.
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

# Avoids an UPDATE on the hot api_keys row per request.
_LAST_USED_THROTTLE = timedelta(minutes=5)

# `Literal` so a typo is a mypy error, not a never-satisfied permission.
WriteFlag = Literal["can_write_budgets", "can_write_policies", "can_approve"]


@dataclass(frozen=True)
class AuthContext:
    """The authenticated principal. Exactly one of `api_key` / `user` is set.

    `perms` is set for humans only; API keys are governed by mode/scope.
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
    if company is None:  # FK-guaranteed; explicit guard, not an assert
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "credential references a missing company",
        )
    return company


def _resolve_api_key(request: Request, token: str, db: Session) -> AuthContext:
    """The SDK path: resolve an API key."""
    limiter = get_limiter()
    ip = client_ip(request)
    # Before the lookup, so a brute-force guess costs no DB round trip.
    limiter.guard_auth_attempt(ip)

    key_hash = hash_api_key(token)
    api_key = db.query(ApiKey).filter(ApiKey.key_hash == key_hash).one_or_none()
    if api_key is None or not api_key.is_active:
        limiter.record_auth_failure(ip)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid API key")

    limiter.check(Bucket.DEFAULT, str(api_key.id))

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
        # 401 triggers the SPA's silent refresh.
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "Invalid or expired session"
        ) from None

    user = db.get(User, claims.user_id)
    # A user moved between companies must not keep the old company's token.
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
        HTTPException: 401 for a missing/unknown/inactive/expired credential;
            429 when the IP or principal exhausts its rate budget.
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
    """A live API key — the money path. Humans and test keys are refused."""
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
    """The tight `expensive` bucket for ledger-walking endpoints.

    Layered on another gate, never instead of one; keys on the principal id.
    """
    identity = (
        f"user:{ctx.user.id}" if ctx.user is not None else str(ctx.api_key.id)  # type: ignore[union-attr]
    )
    get_limiter().check(Bucket.EXPENSIVE, identity)
    return ctx.company


def _require_key_admin(ctx: AuthContext) -> None:
    """The API-key half of every governance gate: live + admin scope."""
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
    """Governance mutation with no finer-grained flag.

    API key: live + admin scope. User: anything but the read-only `auditor`.
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

    API keys are gated as in `require_admin`; humans must hold the flag.

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

    Server-side twin of the sidebar's `perms.nav`. No-op for API keys.

    Args:
        key: A `NavKey`, e.g. `"books"`.

    Returns:
        A dependency yielding the `AuthContext`.
    """

    def dependency(ctx: AuthContext = Depends(get_auth_context)) -> AuthContext:
        if ctx.perms is not None and not ctx.perms.allows(key):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"your role ({ctx.perms.role.value}) cannot access {key}",
            )
        return ctx

    return dependency
