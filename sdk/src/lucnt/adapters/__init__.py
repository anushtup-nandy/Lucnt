"""Per-vendor meter adapters, detected by client module (no vendor SDK imports)."""

from __future__ import annotations

from collections.abc import Mapping

from lucnt.adapters.anthropic import AnthropicAdapter
from lucnt.adapters.base import MeterAdapter, Usage
from lucnt.adapters.openai import OpenAIAdapter
from lucnt.pricing import PriceInput

# Types, not instances: only the matching adapter pays to load pricing.
_ADAPTER_TYPES: tuple[type[MeterAdapter], ...] = (AnthropicAdapter, OpenAIAdapter)


def resolve_adapter(
    client: object,
    *,
    pricing: Mapping[str, tuple[PriceInput, PriceInput]] | None = None,
) -> MeterAdapter | None:
    """Return an adapter that handles `client`, or None if none matches."""
    for adapter_type in _ADAPTER_TYPES:
        if adapter_type.matches(client):
            return adapter_type(pricing=pricing)
    return None


__all__ = ["MeterAdapter", "Usage", "resolve_adapter"]
