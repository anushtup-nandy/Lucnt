"""Per-vendor meter adapters (Phase 3.5 / B).

`resolve_adapter(client)` picks the adapter that handles a given LLM client by
its module name, so `meter()` can wrap it without `lucnt` importing any vendor
SDK. Register new vendors by appending to `_ADAPTERS`.
"""

from __future__ import annotations

from lucnt.adapters.anthropic import AnthropicAdapter
from lucnt.adapters.base import MeterAdapter, Usage
from lucnt.adapters.openai import OpenAIAdapter

# Order is irrelevant — matchers are mutually exclusive by vendor module.
_ADAPTERS: tuple[MeterAdapter, ...] = (AnthropicAdapter(), OpenAIAdapter())


def resolve_adapter(client: object) -> MeterAdapter | None:
    """Return the adapter that handles `client`, or None if none matches."""
    for adapter in _ADAPTERS:
        if adapter.matches(client):
            return adapter
    return None


__all__ = ["MeterAdapter", "Usage", "resolve_adapter"]
