"""Policies router — CRUD for the governance rules the engine evaluates (M5).

Configs are validated against their `rule_type` at write time
(`services.policy_engine.parse_config`) so a malformed rule can never reach the
engine at authorize time. Everything is company-scoped.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from lucnt_api.dependencies import (
    AuthContext,
    get_auth_context,
    get_current_company,
    get_db,
    require_nav,
    require_write,
)
from lucnt_api.models import Agent, Company, Policy, PolicyRuleType, PolicyScopeType
from lucnt_api.pagination import (
    WIDE_MAX_PAGE_SIZE,
    WIDE_PAGE_SIZE,
    Page,
    count_total,
    paginate,
)
from lucnt_api.schemas.policy import (
    PolicyCreate,
    PolicyListResponse,
    PolicyResponse,
    PolicyUpdate,
)
from lucnt_api.services.audit import record as record_audit
from lucnt_api.services.policy_engine import parse_config

# Router-level nav gate (P7.0): a human principal may only reach a domain
# present in `perms.nav` — the same tuple the sidebar renders from, so a
# hidden page and a reachable endpoint cannot drift apart. No-op for API keys.
router = APIRouter(
    prefix="/policies",
    tags=["policies"],
    dependencies=[Depends(require_nav("policies"))],
)


def _to_response(policy: Policy) -> PolicyResponse:
    """Map a Policy ORM row to its API response (config_json -> config)."""
    return PolicyResponse(
        id=policy.id,
        name=policy.name,
        rule_type=policy.rule_type.value,
        config=policy.config_json,
        scope_type=policy.scope_type.value,
        scope_id=policy.scope_id,
        active=policy.active,
        description=policy.description,
        hit_count=policy.hit_count,
        block_count=policy.block_count,
        created_at=policy.created_at,
        updated_at=policy.updated_at,
    )


def _validate_config(rule_type: PolicyRuleType, config: dict[str, object]) -> None:
    """Reject a config that doesn't match its rule type (400, not a 500 later)."""
    try:
        parse_config(rule_type, config)
    except ValidationError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"invalid config for rule_type={rule_type.value!r}: {exc.errors()}",
        ) from exc


def _resolve_scope(
    db: Session, company: Company, payload: PolicyCreate
) -> uuid.UUID | None:
    """Validate scope coherence: agent scope needs a real, in-company agent id."""
    if payload.scope_type == PolicyScopeType.COMPANY:
        return None  # company-wide policies ignore any supplied scope_id
    if payload.scope_id is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "scope_type=agent requires scope_id"
        )
    agent = db.execute(
        select(Agent).where(
            Agent.id == payload.scope_id, Agent.company_id == company.id
        )
    ).scalar_one_or_none()
    if agent is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Unknown agent for scope_id={payload.scope_id}",
        )
    return agent.id


@router.get("", response_model=PolicyListResponse)
def list_policies(
    company: Company = Depends(get_current_company),
    db: Session = Depends(get_db),
    active: bool | None = Query(default=None, description="Filter by active flag"),
    page: Page = Depends(paginate(WIDE_PAGE_SIZE, WIDE_MAX_PAGE_SIZE)),
) -> PolicyListResponse:
    """List policies for the current company."""
    stmt = select(Policy).where(Policy.company_id == company.id)
    if active is not None:
        stmt = stmt.where(Policy.active.is_(active))
    total = count_total(db, stmt)
    rows = (
        db.execute(
            stmt.order_by(Policy.created_at.desc())
            .offset(page.offset)
            .limit(page.limit)
        )
        .scalars()
        .all()
    )
    return PolicyListResponse(items=[_to_response(row) for row in rows], total=total)


@router.post("", response_model=PolicyResponse, status_code=status.HTTP_201_CREATED)
def create_policy(
    payload: PolicyCreate,
    company: Company = Depends(require_write("can_write_policies")),
    db: Session = Depends(get_db),
    ctx: AuthContext = Depends(get_auth_context),
) -> PolicyResponse:
    """Create a policy, validating its config against the rule type first."""
    _validate_config(payload.rule_type, payload.config)
    scope_id = _resolve_scope(db, company, payload)

    policy = Policy(
        company_id=company.id,
        name=payload.name,
        rule_type=payload.rule_type,
        config_json=payload.config,
        scope_type=payload.scope_type,
        scope_id=scope_id,
        active=payload.active,
        description=payload.description,
    )
    db.add(policy)
    db.flush()
    record_audit(
        db,
        ctx,
        action="policy.create",
        entity_type="policy",
        entity_id=policy.id,
        after={
            "name": policy.name,
            "rule_type": policy.rule_type.value,
            "active": policy.active,
            "scope_type": policy.scope_type.value,
        },
    )
    db.commit()
    db.refresh(policy)
    return _to_response(policy)


@router.patch("/{policy_id}", response_model=PolicyResponse)
def update_policy(
    policy_id: uuid.UUID,
    payload: PolicyUpdate,
    company: Company = Depends(require_write("can_write_policies")),
    db: Session = Depends(get_db),
    ctx: AuthContext = Depends(get_auth_context),
) -> PolicyResponse:
    """Update a policy's name/config/active/description (rule_type is immutable)."""
    policy = db.execute(
        select(Policy).where(Policy.id == policy_id, Policy.company_id == company.id)
    ).scalar_one_or_none()
    if policy is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown policy")

    before = {
        "name": policy.name,
        "active": policy.active,
        "config_json": policy.config_json,
    }
    if payload.config is not None:
        _validate_config(policy.rule_type, payload.config)
        policy.config_json = payload.config
    if payload.name is not None:
        policy.name = payload.name
    if payload.active is not None:
        policy.active = payload.active
    if payload.description is not None:
        policy.description = payload.description

    record_audit(
        db,
        ctx,
        action="policy.update",
        entity_type="policy",
        entity_id=policy.id,
        before=before,
        after={
            "name": policy.name,
            "active": policy.active,
            "config_json": policy.config_json,
        },
    )
    db.commit()
    db.refresh(policy)
    return _to_response(policy)
