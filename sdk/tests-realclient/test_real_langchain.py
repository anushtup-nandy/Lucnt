"""F-1 acceptance for the LangChain integration — the finding's original repro.

The 28 Jul probe showed `ChatOpenAI(...).invoke()` spending real money with no
ledger row and no error, because LangChain calls
`client.chat.completions.with_raw_response.create(...)`. The supported
integration is now `http_client=lucnt.metered_http_client(...)`: every request
LangChain's own vendor client makes flows through the governed transport.
"""

from __future__ import annotations

import openai
import pytest
from fake_vendor_server import ServerState
from langchain_openai import ChatOpenAI

import lucnt


def _llm(base_url: str, max_retries: int = 0) -> ChatOpenAI:
    return ChatOpenAI(
        model="gpt-4o",
        api_key="sk-fake",
        base_url=f"{base_url}/v1",
        max_retries=max_retries,
        http_client=lucnt.metered_http_client(
            vendor="openai", agent="researcher", entity="acme-us"
        ),
    )


def test_langchain_invoke_is_governed(harness: tuple[ServerState, str]) -> None:
    """The exact scenario that opened docs/production/plan2.md, now with ledger rows."""
    state, base_url = harness
    output = _llm(base_url).invoke("hello there")
    assert output.content == "governed hello"
    assert len(state.authorizes) == 1
    assert state.authorizes[0]["agent"] == "researcher"
    assert len(state.captures) == 1
    assert state.captures[0]["tokens"] == 150  # settled from real usage


def test_langchain_blocked_spend_never_reaches_the_vendor(
    harness: tuple[ServerState, str],
) -> None:
    state, base_url = harness
    state.block = True
    with pytest.raises(openai.PermissionDeniedError) as excinfo:
        _llm(base_url, max_retries=2).invoke("hello there")
    assert "lucnt blocked" in str(excinfo.value)
    assert state.vendor_calls == []
    assert state.captures == []
    assert len(state.authorizes) == 1  # no vendor-side retry of the refusal


def test_meter_of_a_langchain_wrapper_still_fails_loud(
    harness: tuple[ServerState, str],
) -> None:
    """meter(ChatOpenAI()) is not a supported shape; it must raise, not
    silently half-work (the loud half of F-1, unchanged)."""
    _state, base_url = harness
    llm = ChatOpenAI(model="gpt-4o", api_key="sk-fake", base_url=f"{base_url}/v1")
    with pytest.raises(lucnt.LucntError):
        lucnt.meter(llm, agent="a", entity="e")
