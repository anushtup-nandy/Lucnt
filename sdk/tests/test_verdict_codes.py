"""SDK half of the VerdictCode parity gate: `_BUDGET_EXHAUSTED_CODE` must exist.

Reads a monorepo path on purpose: extracting `sdk/` should fail this, not skip.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lucnt.ledger import _BUDGET_EXHAUSTED_CODE

_VERDICT_CODES = (
    Path(__file__).resolve().parents[2] / "contracts" / "verdict-codes.json"
)


def _committed_codes() -> list[str]:
    if not _VERDICT_CODES.exists():
        pytest.fail(
            f"{_VERDICT_CODES} is missing. In the monorepo, regenerate it with "
            "`cd backend && python scripts/export_contract.py`. Post-split, this "
            "test must be re-pointed at the published contract artifact."
        )
    return list(json.loads(_VERDICT_CODES.read_text())["codes"])


def test_the_hardcoded_budget_code_is_a_real_backend_verdict_code() -> None:
    assert _BUDGET_EXHAUSTED_CODE in _committed_codes()
