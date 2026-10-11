"""Pricing feed router — the server half of the SDK's pricing story.

`GET /pricing/feed` serves the pack the SDK merges over its packaged fallback.
Date-ranged rates are resolved here against today's UTC date, so a price
change (or a lapsing introductory rate) is a server-side data edit, never a
client release. Authenticated with any valid API key: pricing is not a secret,
but an unauthenticated endpoint would be a free amplification target.
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
    """Today's effective vendor pricing, flattened for the SDK.

    The response shape matches the SDK's packaged `pricing.json` exactly
    (`version` / `estimation` / `vendors`), with all money as strings and any
    date-ranged rates already resolved — the client stays date-dumb.
    `Cache-Control` matches the SDK's default client-side TTL so intermediary
    caches and the SDK agree on freshness.
    """
    today = datetime.now(UTC).date()
    return JSONResponse(
        resolve_feed(today),
        headers={"Cache-Control": "public, max-age=3600"},
    )
