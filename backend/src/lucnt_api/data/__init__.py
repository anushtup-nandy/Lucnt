"""Server-owned data packs (domain content, not operator configuration).

`pricing_feed.json` is the server half of F-2: the SDK ships a packaged
pricing pack as its offline fallback, and this feed — served by
`GET /pricing/feed` — is the authority that can change without a client
release. Date-ranged rates live HERE, resolved server-side, so the client
never grows a date engine (docs/scale/SCALABILITY.md §5.7 tier iii).
"""
