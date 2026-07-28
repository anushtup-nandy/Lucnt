"""Fixtures for the real-vendor-client acceptance tests (F-1).

Requires the real `openai`, `anthropic`, and `langchain-openai` packages —
run via `sdk/scripts/run_realclient_tests.sh`, which builds the wheel and
installs everything into a throwaway venv. Not part of the default suite
(`testpaths = ["tests"]`).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fake_vendor_server import ServerState, vendor_server

import lucnt
from lucnt import config as lucnt_config


@pytest.fixture(scope="session")
def _server() -> Iterator[tuple[ServerState, str]]:
    server, state, base_url = vendor_server()
    yield state, base_url
    server.shutdown()


@pytest.fixture()
def harness(_server: tuple[ServerState, str]) -> Iterator[tuple[ServerState, str]]:
    """Fresh server state + lucnt configured against the fake backend."""
    state, base_url = _server
    state.reset()
    lucnt.configure(api_key="sk_harness", base_url=base_url)
    yield state, base_url
    lucnt_config._reset_config()
