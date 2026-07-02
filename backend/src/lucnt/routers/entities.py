"""Entities router."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from lucnt.database import get_db
from lucnt.schemas.entity import EntityOut

router = APIRouter(prefix="/entities", tags=["entities"])


@router.get("", response_model=list[EntityOut])
def list_entities(db: Session = Depends(get_db)) -> list[EntityOut]:  # noqa: ARG001
    """List entities for the current company (Phase 0 stub)."""
    return []


@router.post("/{entity_id}/switch", response_model=dict)
def switch_entity(entity_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:  # noqa: ARG001
    """Switch the active entity context (Phase 0 stub)."""
    return {"message": "Entity switched", "entity_id": entity_id}
