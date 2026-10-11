"""Credential primitives: API keys, human passwords, and dashboard JWTs.

Three different secrets, three deliberately different treatments:

* **API keys** — 32 bytes of generated entropy, so SHA-256 and a direct hash
  lookup. A slow per-record KDF would buy nothing against a secret that cannot
  be guessed, and would cost a scan of every row on the money path.
* **Passwords** — low-entropy and human-chosen, so argon2id, which is memory-hard
  and cannot be looked up. `argon2-cffi` over `python-jose`'s ecosystem for the
  reasons recorded in `pyproject.toml`.
* **Dashboard tokens** — HS256 JWTs signed with `settings.secret_key` (P7.0).
  Access tokens are short-lived and held in the SPA's memory; refresh tokens are
  longer-lived and confined to an httpOnly SameSite=Strict cookie.

**Refresh tokens are stateless, and therefore not revocable before expiry.**
`POST /auth/logout` clears the cookie, which ends the session on that browser,
but a token already copied out of it stays valid for its remaining window. A
revocation list needs a table, and P7.0 is deliberately migration-free. The
mitigation is the 7-day ceiling; revisit at GA alongside the WorkOS/Clerk swap
(D1).
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
    """Generate a new plaintext API key, e.g. `lucnt_live_<url-safe chars>`.

    The plaintext is returned exactly once, to the issuing caller — only its
    hash is ever persisted (see `hash_api_key`).
    """
    return f"lucnt_{mode.value}_{secrets.token_urlsafe(_KEY_NBYTES)}"


def hash_api_key(key: str) -> str:
    """Hash a plaintext API key for storage and lookup.

    SHA-256, not bcrypt: an API key is a 32-byte random secret, not a
    low-entropy password, so it needs direct-lookup by hash rather than a
    slow, per-record salted comparison.
    """
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def looks_like_api_key(token: str) -> bool:
    """True for the `lucnt_live_` / `lucnt_test_` prefixes minted by `generate_api_key`.

    The discriminator for the one `Authorization: Bearer` header serving two
    principal types (`dependencies.get_auth_context`). Prefix-based rather than
    "try JWT, fall back to key lookup": a JWT is three base64 segments joined by
    dots and can never carry this prefix, so the two spaces cannot overlap, and
    a mis-parse here would be an auth bypass rather than a 401.
    """
    return any(token.startswith(f"lucnt_{mode.value}_") for mode in ApiKeyMode)


# --------------------------------------------------------------------------- #
# Passwords (argon2id)
# --------------------------------------------------------------------------- #


def hash_password(plaintext: str) -> str:
    """Hash a human password for storage in `users.password_hash`."""
    return _password_hasher.hash(plaintext)


def verify_password(plaintext: str, hashed: str) -> bool:
    """Constant-time-ish verification. Never raises — a bad hash is a failed login.

    A row whose `password_hash` is corrupt or was written by another scheme must
    not 500 the login endpoint; it must simply fail to authenticate.
    """
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
        # A unique id per token so two tokens minted in the same second for the
        # same user are distinguishable in a log, and so a future revocation list
        # has something to key on.
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
        expected_type: `access` or `refresh`. Checked, not assumed — without it a
            refresh token would be accepted as a bearer credential, silently
            turning the 15-minute access window into a 7-day one.

    Raises:
        InvalidTokenError: signature, expiry, structure, or type mismatch. One
            exception type for all of them: telling a caller *which* part failed
            is free information for an attacker and useless to a real client.
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
