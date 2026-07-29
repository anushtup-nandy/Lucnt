"""Pydantic schemas for the workspace and developer read endpoints (P7.0)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class EntityResponse(BaseModel):
    """A legal entity, as the switcher and the Settings page render it."""

    model_config = ConfigDict(frozen=True, from_attributes=True)

    id: uuid.UUID
    name: str
    code: str
    full_name: str
    currency: str
    accounting_std: str
    erp: str | None
    tax_regime: str
    country: str | None
    timezone: str
    is_active: bool


class EntityListResponse(BaseModel):
    """Response body for `GET /entities`."""

    model_config = ConfigDict(frozen=True)

    items: list[EntityResponse]
    total: int


class ApiKeySummary(BaseModel):
    """An API key as the Developers page may see it: **never the secret**.

    Only the hash is stored, so the plaintext is unrecoverable by construction —
    this schema simply makes that explicit. `key_prefix` is the first 12
    characters (`lucnt_live_8`), which identifies a key without revealing it.
    """

    model_config = ConfigDict(frozen=True, from_attributes=True)

    id: uuid.UUID
    name: str
    key_prefix: str
    mode: str
    scope: str
    is_active: bool
    last_used_at: datetime | None
    created_at: datetime


class ApiKeyListResponse(BaseModel):
    """Response body for `GET /api-keys`."""

    model_config = ConfigDict(frozen=True)

    items: list[ApiKeySummary]
    total: int


class AuditLogEntry(BaseModel):
    """One governance mutation, as the Developers event log renders it."""

    model_config = ConfigDict(frozen=True, from_attributes=True)

    id: uuid.UUID
    action: str
    entity_type: str
    entity_id: uuid.UUID | None
    actor_api_key_id: uuid.UUID | None
    actor_user_id: uuid.UUID | None
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    created_at: datetime


class AuditLogListResponse(BaseModel):
    """Response body for `GET /audit-logs`."""

    model_config = ConfigDict(frozen=True)

    items: list[AuditLogEntry]
    total: int
