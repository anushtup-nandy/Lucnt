"""The pre-call estimate must charge media as media, not as text (T5-10).

The estimate exists only to gate `authorize()`. That makes its failure mode
asymmetric and easy to under-rate: an inflated estimate does not over-bill
anyone, because `capture()` settles the truth — it **blocks a legitimate spend
before it happens**, and the caller sees a policy block with nothing pointing at
the estimate as the cause. So the interesting assertions here are upper bounds.
"""

from __future__ import annotations

from typing import Any

import pytest

from lucnt.adapters.anthropic import AnthropicAdapter
from lucnt.adapters.base import MeterAdapter
from lucnt.adapters.openai import OpenAIAdapter
from lucnt.pricing import load_pricing_pack

# ~750 KB of base64, the shape of one screenshot inlined into a request.
_BIG_B64 = "A" * 750_000


@pytest.fixture
def anthropic_adapter() -> AnthropicAdapter:
    return AnthropicAdapter()


@pytest.fixture
def openai_adapter() -> OpenAIAdapter:
    return OpenAIAdapter()


def _tokens(adapter: MeterAdapter, **kwargs: Any) -> int:
    return adapter._estimate_input_tokens(kwargs)


def test_an_inlined_image_is_not_counted_as_text(
    anthropic_adapter: AnthropicAdapter,
) -> None:
    """The headline fix.

    Before this, `len(str(messages)) // 4` turned a 750 KB base64 payload into
    ~187,000 estimated input tokens. At Opus rates that is a ~$0.94 hold for a
    call whose real prompt is a few thousand tokens — enough to trip an
    amount-threshold rule or exhaust a budget the policy engine believed had room.
    """
    tokens = _tokens(
        anthropic_adapter,
        model="claude-opus-5",
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": _BIG_B64,
                        },
                    },
                    {"type": "text", "text": "What is in this screenshot?"},
                ],
            }
        ],
    )
    pack = load_pricing_pack()
    # One image plus a short question — not a six-figure token count.
    assert tokens < pack.image_tokens * 2
    # And the payload length is genuinely not in the number.
    assert tokens < len(_BIG_B64) // pack.chars_per_token // 10


def test_an_openai_image_url_block_is_not_counted_as_text(
    openai_adapter: OpenAIAdapter,
) -> None:
    """OpenAI spells it `image_url` with a nested data URL — same trap."""
    tokens = _tokens(
        openai_adapter,
        model="gpt-4o",
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "describe"},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{_BIG_B64}"},
                    },
                ],
            }
        ],
    )
    assert tokens < load_pricing_pack().image_tokens * 2


def test_images_scale_linearly(anthropic_adapter: AnthropicAdapter) -> None:
    image = {"type": "image", "source": {"type": "base64", "data": "x"}}

    def with_images(count: int) -> int:
        return _tokens(
            anthropic_adapter,
            model="claude-opus-5",
            messages=[{"role": "user", "content": [image] * count}],
        )

    pack = load_pricing_pack()
    assert with_images(1) == pack.image_tokens
    assert with_images(4) == pack.image_tokens * 4


def test_a_document_is_charged_per_assumed_page(
    anthropic_adapter: AnthropicAdapter,
) -> None:
    pack = load_pricing_pack()
    tokens = _tokens(
        anthropic_adapter,
        model="claude-opus-5",
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {"type": "base64", "data": _BIG_B64},
                    }
                ],
            }
        ],
    )
    assert tokens == pack.document_page_tokens * pack.default_document_pages


def test_an_unrecognised_block_is_charged_as_media_not_as_its_payload(
    anthropic_adapter: AnthropicAdapter,
) -> None:
    """A vendor's new media type must degrade to bounded, not back to base64.

    Charging an unknown block as media is the deliberate choice: it is bounded,
    roughly right for what such a block usually is, and — crucially — it cannot
    reintroduce the six-figure estimate that counting the payload would.
    """
    pack = load_pricing_pack()
    tokens = _tokens(
        anthropic_adapter,
        model="claude-opus-5",
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "video", "source": {"data": _BIG_B64}},
                ],
            }
        ],
    )
    assert tokens == pack.image_tokens


def test_an_unknown_block_carrying_plain_text_counts_that_text(
    anthropic_adapter: AnthropicAdapter,
) -> None:
    pack = load_pricing_pack()
    body = "y" * 4_000
    tokens = _tokens(
        anthropic_adapter,
        model="claude-opus-5",
        messages=[
            {"role": "user", "content": [{"type": "thinking", "text": body}]},
        ],
    )
    assert tokens == len(body) // pack.chars_per_token


def test_plain_text_estimates_are_unchanged(
    anthropic_adapter: AnthropicAdapter,
) -> None:
    """Regression guard: the text path must still be chars // chars_per_token."""
    pack = load_pricing_pack()
    body = "z" * 8_000
    tokens = _tokens(
        anthropic_adapter,
        model="claude-opus-5",
        messages=[{"role": "user", "content": body}],
    )
    assert tokens == len(body) // pack.chars_per_token


def test_a_string_system_prompt_is_counted(
    anthropic_adapter: AnthropicAdapter,
) -> None:
    pack = load_pricing_pack()
    system = "s" * 4_000
    tokens = _tokens(
        anthropic_adapter,
        model="claude-opus-5",
        system=system,
        messages=[{"role": "user", "content": ""}],
    )
    assert tokens == len(system) // pack.chars_per_token


def test_an_anthropic_block_list_system_prompt_is_counted(
    anthropic_adapter: AnthropicAdapter,
) -> None:
    """Anthropic's `system` may be a list of text blocks (for cache_control)."""
    pack = load_pricing_pack()
    body = "s" * 4_000
    tokens = _tokens(
        anthropic_adapter,
        model="claude-opus-5",
        system=[
            {"type": "text", "text": body, "cache_control": {"type": "ephemeral"}}
        ],
        messages=[{"role": "user", "content": ""}],
    )
    assert tokens == len(body) // pack.chars_per_token


def test_tool_schemas_are_counted(anthropic_adapter: AnthropicAdapter) -> None:
    """Tool definitions are real input tokens and were previously ignored."""
    tools = [
        {
            "name": f"tool_{n}",
            "description": "d" * 500,
            "input_schema": {"type": "object", "properties": {}},
        }
        for n in range(10)
    ]
    without = _tokens(
        anthropic_adapter,
        model="claude-opus-5",
        messages=[{"role": "user", "content": "hi"}],
    )
    with_tools = _tokens(
        anthropic_adapter,
        model="claude-opus-5",
        messages=[{"role": "user", "content": "hi"}],
        tools=tools,
    )
    assert with_tools > without + 1_000


def test_a_bare_string_in_a_content_list_is_counted(
    anthropic_adapter: AnthropicAdapter,
) -> None:
    pack = load_pricing_pack()
    body = "q" * 4_000
    tokens = _tokens(
        anthropic_adapter,
        model="claude-opus-5",
        messages=[{"role": "user", "content": [body]}],
    )
    assert tokens == len(body) // pack.chars_per_token


def test_an_empty_request_still_estimates_at_least_one_token(
    anthropic_adapter: AnthropicAdapter,
) -> None:
    """A zero-token estimate would make the authorize gate meaningless."""
    assert _tokens(anthropic_adapter, model="claude-opus-5", messages=[]) == 1


def test_missing_messages_does_not_raise(
    anthropic_adapter: AnthropicAdapter,
) -> None:
    assert _tokens(anthropic_adapter, model="claude-opus-5") == 1


def test_the_estimated_cost_of_an_image_call_is_plausible(
    anthropic_adapter: AnthropicAdapter,
) -> None:
    """End to end, in money: one screenshot must hold cents, not dollars."""
    from decimal import Decimal

    cost = anthropic_adapter.estimate_cost(
        {
            "model": "claude-opus-5",
            "max_tokens": 1_024,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {"type": "base64", "data": _BIG_B64},
                        },
                        {"type": "text", "text": "what is this?"},
                    ],
                }
            ],
        }
    )
    # Opus 5 is $5/1M in, $25/1M out: ~4800 image tokens + 1024 output ≈ $0.05.
    assert cost < Decimal("0.20")
