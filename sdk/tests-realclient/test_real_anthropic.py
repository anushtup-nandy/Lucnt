"""F-1/F-11/F-18 acceptance against the REAL `anthropic` client.

Closes F-18's gap too: the streaming wrappers were only ever asserted against
hand-written fake context managers, because `anthropic` is deliberately not a
dependency of this package. Here the real `MessageStream` machinery parses the
harness's SSE bytes and `get_final_message()` is the real helper.
"""

from __future__ import annotations

import asyncio

import anthropic
from fake_vendor_server import ServerState

import lucnt

_MESSAGES = [{"role": "user", "content": "hello there"}]


def _client(base_url: str) -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key="sk-fake", base_url=base_url, max_retries=0)


def test_messages_create_settles_actual_usage(
    harness: tuple[ServerState, str],
) -> None:
    state, base_url = harness
    client = lucnt.meter(_client(base_url), agent="a", entity="e")
    response = client.messages.create(
        model="claude-opus-5", max_tokens=64, messages=_MESSAGES
    )
    assert response.content[0].text == "governed hello"
    assert len(state.authorizes) == 1
    assert len(state.captures) == 1
    assert state.captures[0]["tokens"] == 59  # 17 in + 42 out, settled


def test_sync_stream_books_from_the_real_final_message(
    harness: tuple[ServerState, str],
) -> None:
    state, base_url = harness
    client = lucnt.meter(_client(base_url), agent="a", entity="e")
    manager = client.messages.stream(
        model="claude-opus-5", max_tokens=64, messages=_MESSAGES
    )
    # F-11: a manager built but not yet entered has minted no hold.
    assert state.authorizes == []
    with manager as stream:
        text = "".join(stream.text_stream)
    assert text == "governed hello"
    assert len(state.authorizes) == 1
    assert len(state.captures) == 1
    assert state.captures[0]["tokens"] == 59  # real get_final_message() usage


def test_count_tokens_is_free_and_passes_through(
    harness: tuple[ServerState, str],
) -> None:
    state, base_url = harness
    client = lucnt.meter(_client(base_url), agent="a", entity="e")
    client.messages.count_tokens(model="claude-opus-5", messages=_MESSAGES)
    assert "/v1/messages/count_tokens" in state.vendor_calls
    assert state.authorizes == []


def test_async_messages_create_settles(harness: tuple[ServerState, str]) -> None:
    state, base_url = harness

    async def main() -> None:
        client = lucnt.meter(
            anthropic.AsyncAnthropic(
                api_key="sk-fake", base_url=base_url, max_retries=0
            ),
            agent="a",
            entity="e",
        )
        response = await client.messages.create(
            model="claude-opus-5", max_tokens=64, messages=_MESSAGES
        )
        assert response.content[0].text == "governed hello"
        await lucnt.aclose()

    asyncio.run(main())
    assert len(state.authorizes) == 1
    assert len(state.captures) == 1
    assert state.captures[0]["tokens"] == 59


def test_async_stream_books_from_the_real_final_message(
    harness: tuple[ServerState, str],
) -> None:
    state, base_url = harness

    async def main() -> None:
        client = lucnt.meter(
            anthropic.AsyncAnthropic(
                api_key="sk-fake", base_url=base_url, max_retries=0
            ),
            agent="a",
            entity="e",
        )
        async with client.messages.stream(
            model="claude-opus-5", max_tokens=64, messages=_MESSAGES
        ) as stream:
            async for _text in stream.text_stream:
                pass
        await lucnt.aclose()

    asyncio.run(main())
    assert len(state.authorizes) == 1
    assert len(state.captures) == 1
    assert state.captures[0]["tokens"] == 59
