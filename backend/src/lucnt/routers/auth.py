"""Auth router."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from lucnt.database import get_db
from lucnt.dependencies import get_current_user
from lucnt.schemas.user import Token
from lucnt.security import create_access_token

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=Token)
def login(username: str, password: str, db: Session = Depends(get_db)) -> Token:
    """Stub login: returns a token for any credentials (Phase 0)."""
    payload = {
        "sub": "00000000-0000-0000-0000-000000000000",
        "email": username,
        "role": "user",
    }
    return Token(access_token=create_access_token(payload))


@router.get("/me", response_model=dict)
def read_me(current_user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    """Return the currently authenticated user payload."""
    return current_user
