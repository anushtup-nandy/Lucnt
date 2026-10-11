"""Pydantic schemas for the dashboard auth router."""

from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict, Field


class LoginRequest(BaseModel):
    """Request body for `POST /auth/login`."""

    model_config = ConfigDict(frozen=True)

    email: str = Field(min_length=3, max_length=255)
    # Bounded so a multi-megabyte body cannot be fed to argon2, which is
    # deliberately expensive per call.
    password: str = Field(min_length=1, max_length=256)


class TokenResponse(BaseModel):
    """Response body for `POST /auth/login` and `POST /auth/refresh`.

    Carries the access token only. The refresh token is set as an httpOnly
    cookie and is deliberately never readable by JavaScript.
    """

    model_config = ConfigDict(frozen=True)

    access_token: str
    token_type: str = "bearer"
    expires_in: int


class NavItemResponse(BaseModel):
    """One sidebar entry. The frontend renders this list; it never hardcodes one."""

    model_config = ConfigDict(frozen=True)

    key: str
    label: str
    path: str


class PermsResponse(BaseModel):
    """Resolved permissions for the signed-in user (`services/permissions.Perms`)."""

    model_config = ConfigDict(frozen=True)

    role: str
    nav: list[NavItemResponse]
    landing: str
    agent_scope: str
    can_write_budgets: bool
    can_write_policies: bool
    can_approve: bool
    can_read_books: bool


class UserResponse(BaseModel):
    """A team member. No PII beyond what the dashboard displays."""

    model_config = ConfigDict(frozen=True, from_attributes=True)

    id: uuid.UUID
    email: str
    name: str
    role: str
    is_active: bool


class UserListResponse(BaseModel):
    """Response body for `GET /users`."""

    model_config = ConfigDict(frozen=True)

    items: list[UserResponse]
    total: int


class MeResponse(BaseModel):
    """Response body for `GET /auth/me` — user, company, and resolved perms."""

    model_config = ConfigDict(frozen=True)

    user: UserResponse
    company_id: uuid.UUID
    company_name: str
    perms: PermsResponse
