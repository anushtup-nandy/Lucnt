"""Server-side pricing feed — resolves date-ranged rates to today's price (F-2).

The SDK ships a packaged pricing pack, which means a price change needs a
client release — and a wrong price books a wrong amount. This service is the
designed replacement (SCALABILITY_PLAN §5.7 tier iii): the server owns the
pricing document, resolves any date-ranged rates against *today*, and serves a
flat pack the SDK merges over its packaged fallback. Date logic lives here so
the client never grows a date engine; the client receives only flat string
prices.

Money in the document is **strings, never JSON numbers** — the same boundary
rule as the SDK pack: a number would arrive client-side as a float, the one
type that cannot represent a price exactly.
"""

from __future__ import annotations

import copy
import json
from datetime import date
from functools import lru_cache
from importlib.resources import files
from typing import Any, Final

_FEED_PACKAGE: Final = "lucnt_api.data"
_FEED_FILENAME: Final = "pricing_feed.json"


@lru_cache(maxsize=1)
def load_feed_document() -> dict[str, Any]:
    """Read and parse the feed document shipped with the backend.

    Raises:
        ValueError: The document is missing or not a JSON object — a broken
            deployment, surfaced loudly rather than served as an empty feed.
    """
    raw = files(_FEED_PACKAGE).joinpath(_FEED_FILENAME).read_text(encoding="utf-8")
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("pricing feed document is not a JSON object")
    return parsed


def _require_price(value: object, where: str) -> str:
    """Validate one price is a string (Decimal-exact on the client side)."""
    if not isinstance(value, str):
        raise ValueError(
            f"pricing feed {where} must be a string, got "
            f"{type(value).__name__} — money is never a JSON number."
        )
    return value


def _parse_day(value: object, where: str) -> date:
    """Parse one ISO date bound of a rate window."""
    if not isinstance(value, str):
        raise ValueError(f"pricing feed {where} must be an ISO date string")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"pricing feed {where} is not an ISO date: {value!r}") from exc


def _resolve_model(
    model: str, entry: dict[str, Any], today: date, where: str
) -> dict[str, Any]:
    """Flatten one model entry to the price effective on `today`.

    A `rates` window matches when `from <= today <= until` (either bound may
    be omitted for an open-ended window); the first matching window wins, and
    the entry's own top-level input/output is the fallback list price. The
    resolved entry carries no `rates` — the client stays date-dumb.
    """
    resolved: dict[str, Any] = {
        "input": _require_price(entry.get("input"), f"{where}.input"),
        "output": _require_price(entry.get("output"), f"{where}.output"),
    }
    if "note" in entry:
        resolved["note"] = entry["note"]
    rates = entry.get("rates", [])
    if not isinstance(rates, list):
        raise ValueError(f"pricing feed {where}.rates must be a list")
    for index, rate in enumerate(rates):
        if not isinstance(rate, dict):
            raise ValueError(f"pricing feed {where}.rates[{index}] must be an object")
        starts = (
            _parse_day(rate["from"], f"{where}.rates[{index}].from")
            if "from" in rate
            else date.min
        )
        ends = (
            _parse_day(rate["until"], f"{where}.rates[{index}].until")
            if "until" in rate
            else date.max
        )
        if starts > ends:
            raise ValueError(f"pricing feed {where}.rates[{index}]: from > until")
        if starts <= today <= ends:
            resolved["input"] = _require_price(
                rate.get("input"), f"{where}.rates[{index}].input"
            )
            resolved["output"] = _require_price(
                rate.get("output"), f"{where}.rates[{index}].output"
            )
            break
    return resolved


def resolve_feed(today: date, document: dict[str, Any] | None = None) -> dict[str, Any]:
    """Produce the flat, client-facing feed effective on `today`.

    Args:
        today: The date to resolve rate windows against (UTC date in the
            router; injectable here so tests never depend on the wall clock).
        document: Override for the shipped document (tests only).

    Returns:
        The SDK pack shape — `version`, `estimation`, `vendors` — with every
        model at exactly one string price pair and no `rates` machinery.

    Raises:
        ValueError: The document is malformed (non-string money, bad window).
    """
    source = document if document is not None else load_feed_document()
    resolved = copy.deepcopy(source)
    vendors = resolved.get("vendors")
    if not isinstance(vendors, dict):
        raise ValueError("pricing feed document has no 'vendors' object")
    for vendor, ventry in vendors.items():
        if not isinstance(ventry, dict) or not isinstance(ventry.get("models"), dict):
            raise ValueError(f"pricing feed vendors.{vendor} is malformed")
        ventry["models"] = {
            model: _resolve_model(
                model, mentry, today, f"vendors.{vendor}.models.{model}"
            )
            for model, mentry in ventry["models"].items()
        }
    return resolved
