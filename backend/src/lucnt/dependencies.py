"""FastAPI dependencies: DB session re-export, current-company resolution."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from lucnt.database import get_db
from lucnt.models import ApiKey, Company
from lucnt.security import hash_api_key

__all__ = ["get_db", "get_current_company"]

_bearer_scheme = HTTPBearer(auto_error=False)


def get_current_company(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    db: Session = Depends(get_db),
) -> Company:
    """Resolve the `Authorization: Bearer <api_key>` header to a `Company`.

    Raises:
        HTTPException: 401 if the header is missing, or the key is unknown or
            inactive. The SDK's `Transport` maps 401/403 to `AuthError`.
    """
    if credentials is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing API key")

    key_hash = hash_api_key(credentials.credentials)
    api_key = db.query(ApiKey).filter(ApiKey.key_hash == key_hash).one_or_none()
    if api_key is None or not api_key.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid API key")

    api_key.last_used_at = datetime.now(timezone.utc)
    db.commit()

    company = db.get(Company, api_key.company_id)
    assert company is not None, "api_keys.company_id is FK-constrained to companies.id"
    return company
