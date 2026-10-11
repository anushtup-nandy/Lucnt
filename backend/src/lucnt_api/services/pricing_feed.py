"""Server-side pricing feed: resolves date-ranged rates to today's flat price.

Date logic stays server-side so the SDK stays date-dumb. Money is always a string;
a JSON number would arrive client-side as a float.
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
        ValueError: The document is not a JSON object (fail loudly, not empty).
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

    First window with `from <= today <= until` wins (bounds optional); otherwise
    the top-level list price.
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

    ``document`` overrides the shipped document (tests only).

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
