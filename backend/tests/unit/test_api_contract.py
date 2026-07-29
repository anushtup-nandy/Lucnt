"""X-6a — the committed contract artifacts must match the code that generates them.

`docs/scale/repo_split.md` §4. Two gates, because one of them cannot be expressed
in OpenAPI at all:

1. `docs/api-contract.json` — a normalized projection of every path, field, type,
   required flag and enum. Catches "someone renamed a response field" without
   going red every time fastapi reorders an `anyOf`.
2. `docs/verdict-codes.json` — the `VerdictCode` value set. `Verdict.code` is an
   open string on the wire by design, so the enum never reaches the OpenAPI
   document; `sdk/src/lucnt/ledger.py` nonetheless branches on the literal
   `"budget_exhausted"` to raise `BudgetExhausted` rather than `PolicyBlocked`.
   Rename that value and OpenAPI shows nothing while every budget block silently
   downgrades. The SDK half of this pair lives in `sdk/tests/test_verdict_codes.py`.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

_BACKEND = Path(__file__).resolve().parents[2]
_SCRIPT = _BACKEND / "scripts" / "export_contract.py"


def _load_exporter() -> ModuleType:
    """Import the generator script, which is not on the package path."""
    spec = importlib.util.spec_from_file_location("export_contract", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["export_contract"] = module
    spec.loader.exec_module(module)
    return module


exporter = _load_exporter()


@pytest.fixture(scope="module")
def document() -> dict[str, object]:
    return exporter.app.openapi()


def test_the_committed_projection_matches_the_code(document: dict[str, object]) -> None:
    committed = json.loads(exporter.CONTRACT_PATH.read_text())
    generated = exporter.build_contract(document)
    assert committed == generated, (
        "docs/api-contract.json is stale. Regenerate with "
        "`cd backend && python scripts/export_contract.py` and review the diff "
        "as a contract change — the SDK and frontend/src/types/api.ts read it."
    )


def test_the_committed_verdict_codes_match_the_enum() -> None:
    committed = json.loads(exporter.VERDICT_CODES_PATH.read_text())
    assert committed["codes"] == exporter.build_verdict_codes()["codes"]


def test_budget_exhausted_is_still_a_verdict_code() -> None:
    """The one code the SDK hardcodes. Renaming it is a silent SDK behaviour change."""
    committed = json.loads(exporter.VERDICT_CODES_PATH.read_text())
    assert "budget_exhausted" in committed["codes"]


def test_generation_is_deterministic(document: dict[str, object]) -> None:
    """A projection that reordered between runs would make the gate noise."""
    assert exporter.build_contract(document) == exporter.build_contract(
        exporter.app.openapi()
    )


def test_the_raw_openapi_artifact_exists(document: dict[str, object]) -> None:
    """Not byte-gated (dependency bumps move it), but it must not be missing.

    `frontend` generates `src/types/api.ts` from this file, so an absent artifact
    breaks the frontend build rather than the backend's.
    """
    assert exporter.OPENAPI_PATH.exists()
    committed = json.loads(exporter.OPENAPI_PATH.read_text())
    assert sorted(committed["paths"]) == sorted(document["paths"])  # type: ignore[index]
