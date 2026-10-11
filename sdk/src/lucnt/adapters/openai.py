"""OpenAI client adapter for `meter()`.

The packaged OpenAI price table is a small starter set; verify it or pass
`meter(pricing=...)` before relying on booked cost.
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
