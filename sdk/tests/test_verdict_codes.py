"""X-6a — the SDK half of the `VerdictCode` parity gate.

`docs/scale/repo_split.md` §4(b). `Verdict.code` is an open string on the wire by
design, so the backend's `VerdictCode` enum never appears in the OpenAPI document
and no schema gate can see it move. `ledger._BUDGET_EXHAUSTED_CODE` is a hardcoded
copy of one of its members, and `_raise_for_verdict` branches on it to choose
`BudgetExhausted` over `PolicyBlocked`. Rename the backend value and both suites
stay green while every budget block downgrades to a generic policy block.

The backend half (`backend/tests/unit/test_api_contract.py`) asserts the committed
value set matches the enum; this asserts the SDK's literal is in that set.

**On the repo split:** this test reads a monorepo path. That is deliberate — when
`sdk/` is extracted (`repo_split.md` §3.2), this test fails loudly rather than
skipping, which is the forcing function for the X-6b/c decision (pin the published
contract artifact, or share a `lucnt-contracts` package).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lucnt.ledger import _BUDGET_EXHAUSTED_CODE

_VERDICT_CODES = Path(__file__).resolve().parents[2] / "docs" / "verdict-codes.json"


def _committed_codes() -> list[str]:
    if not _VERDICT_CODES.exists():
        pytest.fail(
            f"{_VERDICT_CODES} is missing. In the monorepo, regenerate it with "
            "`cd backend && python scripts/export_contract.py`. Post-split, this "
            "test must be re-pointed at the published contract artifact — see "
            "docs/scale/repo_split.md §3.2 (X-6b/c)."
        )
    return list(json.loads(_VERDICT_CODES.read_text())["codes"])


def test_the_hardcoded_budget_code_is_a_real_backend_verdict_code() -> None:
    assert _BUDGET_EXHAUSTED_CODE in _committed_codes()
