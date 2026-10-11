"""OpenAI client adapter for `meter()` (Phase 3.5 / B).

Wraps `client.chat.completions.create(...)`; reads
`response.usage.prompt_tokens` / `completion_tokens` and `response.model`.
Detection is by module so `lucnt` never imports `openai`.

Prices live in the packaged data pack (`lucnt/data/pricing.json`), not here.
⚠️ The OpenAI table is a STARTER SET and intentionally small — OpenAI
prices drift; verify it against current OpenAI pricing and extend it (or pass
`meter(pricing=...)`) for your models before relying on the booked cost.
`max_completion_tokens` is checked in addition to `max_tokens` for the output
estimate.
"""

from __future__ import annotations

from lucnt.adapters.base import MeterAdapter, Usage, _int_attr
from lucnt.exceptions import LucntError


class OpenAIAdapter(MeterAdapter):
    """Adapter for the OpenAI Python SDK's synchronous client."""

    vendor = "openai"
    method_path = ("chat", "completions", "create")
    output_token_keys = ("max_completion_tokens", "max_tokens")

    @classmethod
    def matches(cls, client: object) -> bool:
        """Match any `openai` client by module — `OpenAI` or `AsyncOpenAI`."""
        return type(client).__module__.split(".", 1)[0] == "openai"

    def usage_from_response(self, response: object) -> Usage:
        """Read `.model` + `.usage.{prompt,completion}_tokens` off a chat response."""
        usage = getattr(response, "usage", None)
        model = getattr(response, "model", None)
        if usage is None or not isinstance(model, str):
            raise LucntError("openai: response has no usage/model to meter")
        return Usage(
            model=model,
            input_tokens=_int_attr(usage, "prompt_tokens"),
            output_tokens=_int_attr(usage, "completion_tokens"),
        )
