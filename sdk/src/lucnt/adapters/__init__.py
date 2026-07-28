"""Per-vendor meter adapters (Phase 3.5 / B).

`resolve_adapter(client)` picks the adapter that handles a given LLM client by
its module name, so `meter()` can wrap it without `lucnt` importing any vendor
SDK. Register new vendors by appending to `_ADAPTER_TYPES`.
"""

from __future__ import annotations

from collections.abc import Mapping

from lucnt.adapters.anthropic import AnthropicAdapter
from lucnt.adapters.base import MeterAdapter, Usage
from lucnt.adapters.openai import OpenAIAdapter
from lucnt.pricing import PriceInput

# Types, not instances: `matches` is a classmethod, so detection is free and
# only the winning adapter pays to load the pricing pack and apply overrides.
# Order is irrelevant — matchers are mutually exclusive by vendor module.
_ADAPTER_TYPES: tuple[type[MeterAdapter], ...] = (AnthropicAdapter, OpenAIAdapter)


def resolve_adapter(
    client: object,
    *,
    pricing: Mapping[str, tuple[PriceInput, PriceInput]] | None = None,
) -> MeterAdapter | None:
    """Return an adapter that handles `client`, or None if none matches.

    Args:
        client: The vendor client to detect.
        pricing: Optional price overrides passed to the matched adapter.

    Returns:
        A freshly constructed adapter, or None when no vendor matches.
    """
    for adapter_type in _ADAPTER_TYPES:
        if adapter_type.matches(client):
            return adapter_type(pricing=pricing)
    return None


__all__ = ["MeterAdapter", "Usage", "resolve_adapter"]
