"""Audit-log helper (PRODUCTION_PLAN T0-14).

`record()` appends an audit row within the caller's transaction — it does not
commit — so the trail lands atomically with the mutation it describes. Callers
pass JSON-safe ``before``/``after`` snapshots (money as strings).
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from lucnt_api.models import ApiKey, AuditLog


def record(
    db: Session,
    actor: ApiKey,
    *,
    action: str,
    entity_type: str,
    entity_id: uuid.UUID | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> None:
    """Stage an append-only audit record; the caller's transaction commits it."""
    db.add(
        AuditLog(
            company_id=actor.company_id,
            actor_api_key_id=actor.id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            before=before,
            after=after,
        )
    )
