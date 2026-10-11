"""Audit-log helper.

`record()` appends an audit row within the caller's transaction — it does not
commit — so the trail lands atomically with the mutation it describes. Callers
pass JSON-safe ``before``/``after`` snapshots (money as strings).

P7.0: the actor is now the whole `AuthContext` rather than an `ApiKey`, because a
governance mutation can come from either an SDK key or a signed-in human. The row
records whichever one it was; exactly one actor column is ever set.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy.orm import Session

from lucnt_api.models import AuditLog

if TYPE_CHECKING:  # import-cycle guard: dependencies.py imports nothing from here
    from lucnt_api.dependencies import AuthContext


def record(
    db: Session,
    actor: AuthContext,
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
            company_id=actor.company.id,
            actor_api_key_id=actor.api_key.id if actor.api_key is not None else None,
            actor_user_id=actor.user.id if actor.user is not None else None,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            before=before,
            after=after,
        )
    )
