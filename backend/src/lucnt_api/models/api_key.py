"""ApiKey ORM model — SDK auth, resolves a request to a company.

`key_hash` is SHA-256 of the full secret, not bcrypt: an API key is a
high-entropy generated secret (not a low-entropy human password), so it needs
direct-lookup by hash rather than a slow, per-record salted comparison.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from lucnt_api.database import Base, CreatedAtMixin, UUIDPrimaryKeyMixin


class ApiKeyMode(enum.StrEnum):
    """Live vs. test key, matching the `lucnt_live_...` / `lucnt_test_...` prefix."""

    LIVE = "live"
    TEST = "test"


class ApiKeyScope(enum.StrEnum):
    """What a key may do — separation of duties.

    `runtime` keys drive the money path (authorize/capture) and reads only; they
    are the credential an agent holds and must NOT be able to raise their own
    budget or disable the policy governing them. `admin` keys additionally
    perform governance mutations (budgets, policies, reconciliation, approvals).
    Defaults to `admin` so a key created without a scope keeps full power
    (backward compatible); agent keys are provisioned `runtime` explicitly.
    """

    RUNTIME = "runtime"
    ADMIN = "admin"


class ApiKey(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """A hashed SDK credential that resolves to a company on every request."""

    __tablename__ = "api_keys"

    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"))
    # First 12 chars of the secret, e.g. "lucnt_live_8" -- for display only.
    key_prefix: Mapped[str] = mapped_column(String(12))
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    mode: Mapped[ApiKeyMode] = mapped_column(
        Enum(
            ApiKeyMode,
            name="api_key_mode",
            values_callable=lambda e: [m.value for m in e],
        )
    )
    scope: Mapped[ApiKeyScope] = mapped_column(
        Enum(
            ApiKeyScope,
            name="api_key_scope",
            values_callable=lambda e: [m.value for m in e],
        ),
        default=ApiKeyScope.ADMIN,
        server_default=ApiKeyScope.ADMIN.value,
    )
    is_active: Mapped[bool] = mapped_column(default=True)
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
