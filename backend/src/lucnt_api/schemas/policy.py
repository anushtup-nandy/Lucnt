"""Pydantic schemas for the policies router (Phase 3, M5).

`config` is the rule-specific body validated against `rule_type` by
`services.policy_engine.parse_config` in the router — never trusted raw.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from lucnt_api.models.policy import PolicyRuleType, PolicyScopeType


class PolicyCreate(BaseModel):
    """Request body for `POST /policies`."""

    model_config = ConfigDict(frozen=True)

    name: str
    rule_type: PolicyRuleType
    # Heterogeneous per rule_type; validated via parse_config in the router.
    config: dict[str, Any]
    scope_type: PolicyScopeType = PolicyScopeType.COMPANY
    scope_id: uuid.UUID | None = None
    active: bool = True
    description: str | None = None


class PolicyUpdate(BaseModel):
    """Request body for `PATCH /policies/{id}` — all fields optional.

    `rule_type` and scope are immutable (they define the policy's identity);
    change them by deleting and recreating.
    """

    model_config = ConfigDict(frozen=True)

    name: str | None = None
    config: dict[str, Any] | None = None
    active: bool | None = None
    description: str | None = None


class PolicyResponse(BaseModel):
    """Response body for the policies router."""

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    name: str
    rule_type: str
    config: dict[str, Any]
    scope_type: str
    scope_id: uuid.UUID | None
    active: bool
    description: str | None
    hit_count: int
    block_count: int
    created_at: datetime
    updated_at: datetime


class PolicyListResponse(BaseModel):
    """Response body for `GET /policies`."""

    model_config = ConfigDict(frozen=True)

    items: list[PolicyResponse]
    total: int
