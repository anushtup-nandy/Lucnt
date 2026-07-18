"""API-key generation and hashing (PLAN.md §3: `api_keys`)."""

from __future__ import annotations

import hashlib
import secrets

from lucnt_api.models import ApiKeyMode

_KEY_NBYTES = 32


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
