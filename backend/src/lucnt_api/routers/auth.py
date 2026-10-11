"""Dashboard auth: login, refresh, logout, me.

Access token lives in SPA memory only (never localStorage); the refresh token is
an httpOnly SameSite=Strict cookie. Login shares the per-IP auth-failure limiter.
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
        HTTPException: 401 with one message for every failure (no account
            enumeration); 429 when the IP's failed-attempt budget is spent.
    """
    limiter = get_limiter()
    limiter.guard_auth_attempt(request_ip)

    user = db.execute(
        select(User).where(User.email == payload.email.strip().lower())
    ).scalar_one_or_none()
    # Inactive users still hit verify_password, keeping failure timings similar.
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

    Raises:
        HTTPException: 401 if the cookie is absent, invalid, expired, or names a
            deactivated user.
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
    """Clear the refresh cookie; a copied refresh token is not revoked (stateless)."""
    response.delete_cookie(REFRESH_COOKIE, path="/auth")


@router.get("/me", response_model=MeResponse)
def me(ctx: AuthContext = Depends(get_auth_context)) -> MeResponse:
    """The signed-in user, their company, and their resolved permissions.

    Raises:
        HTTPException: 400 when called with an API key (valid but wrong kind).
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
