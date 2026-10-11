"""Dashboard auth: login, refresh, logout, me.

Hand-rolled JWT with the `resolve_perms()` seam kept (D1). WorkOS/Clerk is a GA
decision, not a demo dependency — and it would still require this seam.

**Token placement is the security design, not a preference.**

* The **access token** is returned in the body and held in the SPA's memory
  only. `localStorage` is readable by any injected script; a token that survives
  a tab close buys convenience and sells the session.
* The **refresh token** is set as an httpOnly, `SameSite=Strict` cookie, so
  JavaScript cannot read it and a cross-site request cannot send it. `Secure` is
  set outside development, where the dashboard is served over plain HTTP.

Login is rate-limited on the caller's IP through the existing auth-failure
bucket — the same one that guards API-key brute force — because a login
endpoint is the cheapest password-guessing oracle in the product.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Cookie, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from lucnt_api.config import settings
from lucnt_api.database import get_db
from lucnt_api.dependencies import AuthContext, get_auth_context
from lucnt_api.models.user import User
from lucnt_api.rate_limit import client_ip, get_limiter
from lucnt_api.schemas.auth import (
    LoginRequest,
    MeResponse,
    NavItemResponse,
    PermsResponse,
    TokenResponse,
    UserResponse,
)
from lucnt_api.security import (
    InvalidTokenError,
    decode_token,
    mint_access_token,
    mint_refresh_token,
    verify_password,
)
from lucnt_api.services.permissions import Perms

router = APIRouter(prefix="/auth", tags=["auth"])

REFRESH_COOKIE = "lucnt_refresh"


def _set_refresh_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        REFRESH_COOKIE,
        token,
        httponly=True,
        samesite="strict",
        secure=settings.environment != "development",
        max_age=settings.refresh_token_ttl_days * 24 * 60 * 60,
        path="/auth",
    )


def _perms_response(perms: Perms) -> PermsResponse:
    return PermsResponse(
        role=perms.role.value,
        nav=[
            NavItemResponse(key=item.key, label=item.label, path=item.path)
            for item in perms.nav
        ],
        landing=perms.landing,
        agent_scope=perms.agent_scope,
        can_write_budgets=perms.can_write_budgets,
        can_write_policies=perms.can_write_policies,
        can_approve=perms.can_approve,
        can_read_books=perms.can_read_books,
    )


def _issue(response: Response, user: User) -> TokenResponse:
    _set_refresh_cookie(response, mint_refresh_token(user.id, user.company_id))
    return TokenResponse(
        access_token=mint_access_token(user.id, user.company_id),
        expires_in=settings.access_token_ttl_minutes * 60,
    )


@router.post("/login", response_model=TokenResponse)
def login(
    payload: LoginRequest,
    response: Response,
    request_ip: str = Depends(client_ip),
    db: Session = Depends(get_db),
) -> TokenResponse:
    """Exchange email + password for an access token and a refresh cookie.

    Raises:
        HTTPException: 401 for any failure — unknown email, wrong password, or
            a deactivated account. One message for all three, deliberately:
            distinguishing them turns the endpoint into an account enumerator.
            429 when the IP has exhausted its failed-attempt budget.
    """
    limiter = get_limiter()
    limiter.guard_auth_attempt(request_ip)

    user = db.execute(
        select(User).where(User.email == payload.email.strip().lower())
    ).scalar_one_or_none()
    # `verify_password` is still called against a real hash when the user exists
    # but is inactive, so the three failure modes cost roughly the same time.
    if (
        user is None
        or not user.is_active
        or not verify_password(payload.password, user.password_hash)
    ):
        limiter.record_auth_failure(request_ip)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password")

    user.last_login = datetime.now(UTC)
    db.commit()
    return _issue(response, user)


@router.post("/refresh", response_model=TokenResponse)
def refresh(
    response: Response,
    request_ip: str = Depends(client_ip),
    db: Session = Depends(get_db),
    lucnt_refresh: str | None = Cookie(default=None, alias=REFRESH_COOKIE),
) -> TokenResponse:
    """Mint a new access token from the refresh cookie, rotating the cookie.

    The refresh token is re-issued on every use so a stolen cookie's window is
    bounded by the next legitimate refresh rather than by the full 7 days.

    Raises:
        HTTPException: 401 if the cookie is absent, invalid, expired, or names a
            user who has since been deactivated.
    """
    if not lucnt_refresh:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "No session")
    limiter = get_limiter()
    limiter.guard_auth_attempt(request_ip)
    try:
        claims = decode_token(lucnt_refresh, "refresh")
    except InvalidTokenError:
        limiter.record_auth_failure(request_ip)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session expired") from None

    user = db.get(User, claims.user_id)
    if user is None or not user.is_active or user.company_id != claims.company_id:
        limiter.record_auth_failure(request_ip)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session expired")
    return _issue(response, user)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(response: Response) -> None:
    """Clear the refresh cookie.

    This ends the session on this browser. It does **not** revoke a refresh
    token already copied out of the cookie — the token is stateless and P7.0 has
    no revocation table (see `security.py`). The 7-day ceiling is the mitigation.
    """
    response.delete_cookie(REFRESH_COOKIE, path="/auth")


@router.get("/me", response_model=MeResponse)
def me(ctx: AuthContext = Depends(get_auth_context)) -> MeResponse:
    """The signed-in user, their company, and their resolved permissions.

    The SPA's first call after login: `perms.nav` builds the sidebar and
    `perms.landing` decides where to land. Both are data, never hardcoded.

    Raises:
        HTTPException: 400 when called with an API key, which has no user, no
            role and therefore no nav. Not 403 — the credential is valid, it is
            simply the wrong *kind* for this endpoint.
    """
    if ctx.user is None or ctx.perms is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "/auth/me describes a human session; this credential is an API key",
        )
    return MeResponse(
        user=UserResponse.model_validate(
            {
                "id": ctx.user.id,
                "email": ctx.user.email,
                "name": ctx.user.name,
                "role": ctx.user.role.value,
                "is_active": ctx.user.is_active,
            }
        ),
        company_id=ctx.company.id,
        company_name=ctx.company.name,
        perms=_perms_response(ctx.perms),
    )
