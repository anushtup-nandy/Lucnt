"""Policies schemas. `config` is typed per `rule_type` by `parse_config`."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from lucnt_api.constants import (
    NAME_MAX,
    NOTES_MAX,
    POLICY_CONFIG_MAX_KEYS,
    POLICY_CONFIG_VALUE_MAX,
)
from lucnt_api.models.policy import PolicyRuleType, PolicyScopeType


def _bound_config(value: dict[str, Any]) -> dict[str, Any]:
    """Cap key count and per-value length of a raw `config` (defence in depth)."""
    if len(value) > POLICY_CONFIG_MAX_KEYS:
        raise ValueError(f"config accepts at most {POLICY_CONFIG_MAX_KEYS} keys")
    for key, item in value.items():
        if len(str(item)) > POLICY_CONFIG_VALUE_MAX:
            raise ValueError(
                f"config value for {key!r} exceeds {POLICY_CONFIG_VALUE_MAX} characters"
            )
    return value


# Bound lives on the type so every request body gets it.
PolicyConfig = Annotated[dict[str, Any], AfterValidator(_bound_config)]


class PolicyCreate(BaseModel):
    """Request body for `POST /policies`."""

    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1, max_length=NAME_MAX)
    rule_type: PolicyRuleType
    config: PolicyConfig
    scope_type: PolicyScopeType = PolicyScopeType.COMPANY
    scope_id: uuid.UUID | None = None
    active: bool = True
    description: str | None = Field(default=None, max_length=NOTES_MAX)


class PolicyUpdate(BaseModel):
    """`PATCH /policies/{id}`. `rule_type` and scope are immutable."""

    model_config = ConfigDict(frozen=True)

    name: str | None = Field(default=None, min_length=1, max_length=NAME_MAX)
    config: PolicyConfig | None = None
    active: bool | None = None
    description: str | None = Field(default=None, max_length=NOTES_MAX)


class PolicyResponse(BaseModel):
    """Response body for the policies router."""

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    name: str
    rule_type: str
    # Unbounded on read: older rows may exceed the input cap.
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
