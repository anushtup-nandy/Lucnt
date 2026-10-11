"""Pricing feed router: the pack the SDK merges over its packaged fallback.

Auth required: pricing is not secret, but an open endpoint is an amplification target.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from lucnt_api.dependencies import get_current_company
from lucnt_api.models import Company
from lucnt_api.services.pricing_feed import resolve_feed

router = APIRouter(prefix="/pricing", tags=["pricing"])


@router.get("/feed")
def get_pricing_feed(
    company: Company = Depends(get_current_company),
) -> JSONResponse:
    """Today's effective vendor pricing, date ranges resolved, in the SDK's shape.

    `Cache-Control` matches the SDK's default client-side TTL.
    """
    today = datetime.now(UTC).date()
    return JSONResponse(
        resolve_feed(today),
        headers={"Cache-Control": "public, max-age=3600"},
    )
