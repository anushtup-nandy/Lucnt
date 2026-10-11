"""Acceptance against the REAL `openai` client.

Every row of the coverage table that was ❌-silent must now show ledger
traffic; the blocked case must surface typed, loud, and unretried.
"""

from __future__ import annotations

import openai
import pytest
from fake_vendor_server import ServerState

import lucnt


def _client(base_url: str, max_retries: int = 0) -> openai.OpenAI:
    return openai.OpenAI(
        api_key="sk-fake", base_url=f"{base_url}/v1", max_retries=max_retries
    )


_MESSAGES = [{"role": "user", "content": "hello there"}]


def test_wrapped_chat_path_settles_actual_usage(
    harness: tuple[ServerState, str],
) -> None:
    state, base_url = harness
    client = lucnt.meter(_client(base_url), agent="a", entity="e")
    response = client.chat.completions.create(model="gpt-4o", messages=_MESSAGES)
    assert response.choices[0].message.content == "governed hello"
    assert len(state.authorizes) == 1
    assert len(state.captures) == 1
    assert state.captures[0]["tokens"] == 150  # settled, not the estimate


def test_with_raw_response_is_governed(harness: tuple[ServerState, str]) -> None:
    """THE headline path: LangChain calls `with_raw_response.create`,
    which used to pass the method wrappers entirely — spend, no ledger row."""
    state, base_url = harness
    client = lucnt.meter(_client(base_url), agent="a", entity="e")
    raw = client.chat.completions.with_raw_response.create(
        model="gpt-4o", messages=_MESSAGES
    )
    assert raw.parse().choices[0].message.content == "governed hello"
    assert len(state.authorizes) == 1
    assert len(state.captures) == 1


def test_with_streaming_response_is_governed(
    harness: tuple[ServerState, str],
) -> None:
    state, base_url = harness
    client = lucnt.meter(_client(base_url), agent="a", entity="e")
    with client.chat.completions.with_streaming_response.create(
        model="gpt-4o", messages=_MESSAGES
    ) as response:
        response.read()
    assert len(state.authorizes) == 1
    assert len(state.captures) == 1


def test_responses_api_is_governed(harness: tuple[ServerState, str]) -> None:
    """The Responses API is where OpenAI is steering new work."""
    state, base_url = harness
    client = lucnt.meter(_client(base_url), agent="a", entity="e")
    response = client.responses.create(model="gpt-4o", input="hello there")
    assert response.usage is not None
    assert len(state.authorizes) == 1
    assert len(state.captures) == 1
    assert state.captures[0]["tokens"] == 123


def test_embeddings_are_governed(harness: tuple[ServerState, str]) -> None:
    """Embeddings are real spend and were entirely invisible."""
    state, base_url = harness
    client = lucnt.meter(_client(base_url), agent="a", entity="e")
    client.embeddings.create(model="text-embedding-3-small", input="hello")
    assert len(state.authorizes) == 1
    assert len(state.captures) == 1
    assert state.captures[0]["model"] == "text-embedding-3-small"
    assert state.captures[0]["tokens"] == 8


def test_beta_stream_helper_is_governed(harness: tuple[ServerState, str]) -> None:
    state, base_url = harness
    client = lucnt.meter(_client(base_url), agent="a", entity="e")
    with client.beta.chat.completions.stream(
        model="gpt-4o", messages=_MESSAGES
    ) as stream:
        for _event in stream:
            pass
    assert len(state.authorizes) == 1
    assert len(state.captures) == 1


def test_stream_true_on_create_is_governed(
    harness: tuple[ServerState, str],
) -> None:
    state, base_url = harness
    client = lucnt.meter(_client(base_url), agent="a", entity="e")
    chunks = list(
        client.chat.completions.create(
            model="gpt-4o",
            messages=_MESSAGES,
            stream=True,
            stream_options={"include_usage": True},
        )
    )
    assert chunks, "the caller's stream must still deliver chunks"
    assert len(state.captures) == 1
    assert state.captures[0]["tokens"] == 15  # from the usage chunk


def test_blocked_spend_is_typed_loud_and_unretried(
    harness: tuple[ServerState, str],
) -> None:
    """A blocked verdict on a governed path surfaces as the vendor's own 403
    error carrying the lucnt reason — with retries LEFT ON to prove the vendor
    SDK does not retry it (probe-verified design constraint)."""
    state, base_url = harness
    state.block = True
    client = lucnt.meter(_client(base_url, max_retries=2), agent="a", entity="e")
    with pytest.raises(openai.PermissionDeniedError) as excinfo:
        client.chat.completions.with_raw_response.create(
            model="gpt-4o", messages=_MESSAGES
        )
    assert "lucnt blocked" in str(excinfo.value)
    assert state.vendor_calls == []  # never reached the vendor
    assert state.captures == []  # no money moved
    assert len(state.authorizes) == 1  # asked policy exactly once — no retries


def test_unknown_spend_path_fails_closed(harness: tuple[ServerState, str]) -> None:
    """A vendor endpoint lucnt cannot price is refused, not silently forwarded.
    The fake server would answer 200 for it, so only the governor stands
    between this call and ungoverned spend."""
    state, base_url = harness
    client = lucnt.meter(_client(base_url), agent="a", entity="e")
    with pytest.raises(openai.PermissionDeniedError) as excinfo:
        client.audio.speech.create(model="tts-1", voice="alloy", input="hi")
    assert "lucnt cannot meter" in str(excinfo.value)
    assert state.vendor_calls == []
    assert state.authorizes == []


def test_reads_pass_through(harness: tuple[ServerState, str]) -> None:
    state, base_url = harness
    client = lucnt.meter(_client(base_url), agent="a", entity="e")
    client.models.list()
    assert state.authorizes == []
    assert state.captures == []
