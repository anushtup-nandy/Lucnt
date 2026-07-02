"""Security utilities."""

from __future__ import annotations

import base64
import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
from jose import JWTError, jwt

from lucnt.config import settings

ALGORITHM = "HS256"


def _prehash(password: str) -> bytes:
    """Reduce a password to a fixed 44-byte digest for bcrypt.

    bcrypt silently ignores input beyond 72 bytes; pre-hashing with SHA-256
    (base64-encoded to 44 bytes) removes that limit and null-byte truncation
    without weakening the scheme.
    """
    digest = hashlib.sha256(password.encode("utf-8")).digest()
    return base64.b64encode(digest)


def create_access_token(
    data: dict[str, Any], expires_delta: timedelta | None = None
) -> str:
    """Create a JWT access token."""
    to_encode = data.copy()
    delta = expires_delta or timedelta(
        minutes=settings.access_token_expire_minutes
    )
    expire = datetime.now(timezone.utc) + delta
    to_encode.update({"exp": expire})
    return str(jwt.encode(to_encode, settings.secret_key, algorithm=ALGORITHM))


def verify_token(token: str) -> dict[str, Any] | None:
    """Verify a JWT token and return its payload."""
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])
        return payload  # type: ignore[no-any-return]
    except JWTError:
        return None


def get_password_hash(password: str) -> str:
    """Hash a plain text password with bcrypt."""
    hashed = bcrypt.hashpw(_prehash(password), bcrypt.gensalt())
    return hashed.decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a plain text password against a bcrypt hash."""
    try:
        return bcrypt.checkpw(
            _prehash(plain_password), hashed_password.encode("utf-8")
        )
    except ValueError:
        # Malformed stored hash — treat as a failed verification, never raise.
        return False
