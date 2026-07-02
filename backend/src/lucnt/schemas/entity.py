"""Entity Pydantic schemas."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class EntityBase(BaseModel):
    """Base entity fields."""

    name: str
    code: str
    full_name: str | None = None
    currency: str = "USD"
    accounting_std: str | None = None
    erp: str | None = None
    tax_regime: str | None = None
    to_usd_rate: float = 1.0
    timezone: str = "UTC"
    is_active: bool = True

    model_config = ConfigDict(from_attributes=True)


class EntityCreate(EntityBase):
    """Schema for creating an entity."""

    pass


class EntityOut(EntityBase):
    """Schema for returning an entity."""

    id: uuid.UUID
    company_id: uuid.UUID
    created_at: datetime
