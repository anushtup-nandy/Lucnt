"""User Pydantic schemas."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr


class UserBase(BaseModel):
    """Base user fields."""

    email: EmailStr
    name: str
    role: str = "user"

    model_config = ConfigDict(from_attributes=True)


class UserCreate(UserBase):
    """Schema for creating a user."""

    password: str


class UserOut(UserBase):
    """Schema for returning a user."""

    id: uuid.UUID
    company_id: uuid.UUID
    is_active: bool
    created_at: datetime


class Token(BaseModel):
    """JWT token schema."""

    access_token: str
    token_type: str = "bearer"
