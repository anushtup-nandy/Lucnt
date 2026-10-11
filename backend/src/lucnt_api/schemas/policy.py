"""Pydantic schemas for the policies router (Phase 3, M5).

`config` is the rule-specific body validated against `rule_type` by
`services.policy_engine.parse_config` in the router — never trusted raw.
"""

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
    """Cap the size of a raw `config` blob before `parse_config` types it.

    `parse_config` already rejects unknown keys per rule type (`extra="forbid"`),
    so this is defence in depth on the *shape* rather than the content: it stops
    an arbitrarily large JSON object being accepted, and keeps any single scalar
    from carrying an unbounded string into storage. Nested containers are counted
    by their serialized length, not walked — the typed configs are flat.
    """
    if len(value) > POLICY_CONFIG_MAX_KEYS:
        raise ValueError(f"config accepts at most {POLICY_CONFIG_MAX_KEYS} keys")
    for key, item in value.items():
        if len(str(item)) > POLICY_CONFIG_VALUE_MAX:
            raise ValueError(
                f"config value for {key!r} exceeds {POLICY_CONFIG_VALUE_MAX} characters"
            )
    return value


# The bound travels with the type, so both request bodies get it by construction
# rather than by remembering to attach a validator.
PolicyConfig = Annotated[dict[str, Any], AfterValidator(_bound_config)]


class PolicyCreate(BaseModel):
    """Request body for `POST /policies`."""

    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1, max_length=NAME_MAX)
    rule_type: PolicyRuleType
    # Heterogeneous per rule_type; validated via parse_config in the router.
    config: PolicyConfig
    scope_type: PolicyScopeType = PolicyScopeType.COMPANY
    scope_id: uuid.UUID | None = None
    active: bool = True
    description: str | None = Field(default=None, max_length=NOTES_MAX)


class PolicyUpdate(BaseModel):
    """Request body for `PATCH /policies/{id}` — all fields optional.

    `rule_type` and scope are immutable (they define the policy's identity);
    change them by deleting and recreating.
    """

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
    # Deliberately unbounded: this is a *read*. Applying the input cap here would
    # turn a row stored before the bound existed into a 500 on serialization.
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
