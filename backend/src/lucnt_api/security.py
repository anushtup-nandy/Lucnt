"""Credential primitives: API keys (SHA-256), passwords (argon2id), JWTs (HS256).

Refresh tokens are stateless, so not revocable before expiry; logout only
clears the cookie. Mitigated by the 7-day TTL.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import Argon2Error, VerifyMismatchError

from lucnt_api.config import settings
from lucnt_api.models import ApiKeyMode

_KEY_NBYTES = 32

_JWT_ALGORITHM = "HS256"
_password_hasher = PasswordHasher()

TokenType = Literal["access", "refresh"]


class InvalidTokenError(Exception):
    """A token that is absent, malformed, expired, or of the wrong type."""


def generate_api_key(mode: ApiKeyMode) -> str:
    """Generate a plaintext API key (`lucnt_live_...`); only its hash is stored."""
    return f"lucnt_{mode.value}_{secrets.token_urlsafe(_KEY_NBYTES)}"


def hash_api_key(key: str) -> str:
    """Hash an API key for lookup; SHA-256 suffices for 32 random bytes."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def looks_like_api_key(token: str) -> bool:
    """True for the `lucnt_live_` / `lucnt_test_` prefixes minted by `generate_api_key`.

    Discriminates API keys from JWTs on the shared Bearer header; the prefix
    spaces cannot overlap, unlike a try-JWT-then-key fallback.
    """
    return any(token.startswith(f"lucnt_{mode.value}_") for mode in ApiKeyMode)


# --------------------------------------------------------------------------- #
# Passwords (argon2id)
# --------------------------------------------------------------------------- #


def hash_password(plaintext: str) -> str:
    """Hash a human password for storage in `users.password_hash`."""
    return _password_hasher.hash(plaintext)


def verify_password(plaintext: str, hashed: str) -> bool:
    """Constant-time-ish verification. Never raises — a bad hash is a failed login."""
    try:
        return _password_hasher.verify(hashed, plaintext)
    except (VerifyMismatchError, Argon2Error):
        return False


# --------------------------------------------------------------------------- #
# Dashboard JWTs
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class TokenClaims:
    """The claims this application actually reads back out of a token."""

    user_id: uuid.UUID
    company_id: uuid.UUID
    token_type: TokenType


def _mint(user_id: uuid.UUID, company_id: uuid.UUID, token_type: TokenType) -> str:
    now = datetime.now(UTC)
    lifetime = (
        timedelta(minutes=settings.access_token_ttl_minutes)
        if token_type == "access"
        else timedelta(days=settings.refresh_token_ttl_days)
    )
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "cid": str(company_id),
        "typ": token_type,
        "iat": now,
        "exp": now + lifetime,
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(payload, settings.secret_key, algorithm=_JWT_ALGORITHM)


def mint_access_token(user_id: uuid.UUID, company_id: uuid.UUID) -> str:
    """A short-lived token the SPA holds in memory only (never localStorage)."""
    return _mint(user_id, company_id, "access")


def mint_refresh_token(user_id: uuid.UUID, company_id: uuid.UUID) -> str:
    """A longer-lived token confined to an httpOnly SameSite=Strict cookie."""
    return _mint(user_id, company_id, "refresh")


def decode_token(token: str, expected_type: TokenType) -> TokenClaims:
    """Verify a token's signature, expiry and *type*, and return its claims.

    Args:
        token: The raw compact JWT.
        expected_type: `access` or `refresh`; stops a refresh token posing as
            an access token.

    Raises:
        InvalidTokenError: Any failure; deliberately undifferentiated.
    """
    try:
        payload = jwt.decode(
            token,
            settings.secret_key,
            algorithms=[_JWT_ALGORITHM],
            options={"require": ["exp", "iat", "sub"]},
        )
    except jwt.PyJWTError as exc:
        raise InvalidTokenError(str(exc)) from exc

    if payload.get("typ") != expected_type:
        raise InvalidTokenError(f"expected a {expected_type} token")
    try:
        return TokenClaims(
            user_id=uuid.UUID(payload["sub"]),
            company_id=uuid.UUID(payload["cid"]),
            token_type=expected_type,
        )
    except (KeyError, ValueError) as exc:
        raise InvalidTokenError("token claims are malformed") from exc
