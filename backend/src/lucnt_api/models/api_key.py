"""ApiKey ORM model: SDK credential that resolves a request to a company.

`key_hash` is unsalted SHA-256: keys are high-entropy, so direct hash lookup is safe.
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
    """What a key may do (separation of duties).

    `runtime`: money path + reads only, so an agent can't raise its own budget or
    disable its policy. `admin`: also governance mutations. Defaults to `admin`.
    """

    RUNTIME = "runtime"
    ADMIN = "admin"


class ApiKey(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """A hashed SDK credential that resolves to a company on every request."""

    __tablename__ = "api_keys"

    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"))
    # First 12 chars of the secret, display only.
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
