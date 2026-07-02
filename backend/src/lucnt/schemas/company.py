"""Company Pydantic schemas."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class CompanyBase(BaseModel):
    """Base company fields."""

    name: str
    slug: str

    model_config = ConfigDict(from_attributes=True)


class CompanyCreate(CompanyBase):
    """Schema for creating a company."""

    pass


class CompanyOut(CompanyBase):
    """Schema for returning a company."""

    id: uuid.UUID
    plan: str = "free"
    created_at: datetime
