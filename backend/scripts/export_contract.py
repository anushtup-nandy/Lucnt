"""Export the API contract artifacts to `contracts/`.

Three artifacts, two of which are gated:

| Artifact | Gate? | Purpose |
|---|---|---|
| `contracts/api-contract.json` | yes | projection: path, field, type, required, enum |
| `contracts/verdict-codes.json` | yes | `VerdictCode` set — never reaches OpenAPI |
| `contracts/openapi.json` | no | raw doc; feeds `openapi-typescript` → frontend types |

The projection (not a byte-diff) ignores cosmetic churn from fastapi/pydantic
bumps. Verdict codes are gated separately: they never reach OpenAPI, yet the SDK
branches on `"budget_exhausted"`.

Usage::

    python scripts/export_contract.py            # regenerate all three
    python scripts/export_contract.py --check    # fail if the gated two drifted
"""

from __future__ import annotations

import argparse
import difflib
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lucnt_api.main import app  # noqa: E402
from lucnt_api.services.policy_engine import VerdictCode  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = _REPO_ROOT / "contracts"
OPENAPI_PATH = CONTRACTS / "openapi.json"
CONTRACT_PATH = CONTRACTS / "api-contract.json"
VERDICT_CODES_PATH = CONTRACTS / "verdict-codes.json"

_SCHEMA_REF_PREFIX = "#/components/schemas/"
_METHODS = ("get", "post", "put", "patch", "delete")


def _ref_name(ref: str) -> str:
    """`#/components/schemas/Agent` -> `Agent`."""
    return ref.removeprefix(_SCHEMA_REF_PREFIX)


def _type_of(node: Any) -> str:
    """Render a JSON-Schema node as a stable, order-independent type string."""
    if not isinstance(node, dict):
        return "any"
    if "$ref" in node:
        return _ref_name(node["$ref"])
    for key, joiner in (("anyOf", "|"), ("oneOf", "|"), ("allOf", "&")):
        if key in node:
            return joiner.join(sorted({_type_of(member) for member in node[key]}))
    node_type = node.get("type")
    if node_type == "array":
        return f"array<{_type_of(node.get('items', {}))}>"
    if node_type == "object":
        extra = node.get("additionalProperties")
        if isinstance(extra, dict):
            return f"map<{_type_of(extra)}>"
        return "object"
    if node_type is None:
        return "any"
    fmt = node.get("format")
    return f"{node_type}({fmt})" if fmt else str(node_type)


def _enum_of(node: dict[str, Any]) -> list[str] | None:
    """Sorted enum members, or `None`. Sorted because member order is not contract."""
    values = node.get("enum")
    if values is None:
        return None
    return sorted(str(value) for value in values)


def _project_schema(schema: dict[str, Any]) -> Any:
    """Project a schema to `{field: {type, required, enum}}`; enums to members."""
    enum_values = _enum_of(schema)
    if enum_values is not None:
        return {"enum": enum_values, "type": _type_of({"type": schema.get("type")})}
    required = set(schema.get("required", ()))
    properties = schema.get("properties", {})
    return {
        name: {
            "type": _type_of(node),
            "required": name in required,
            "enum": _enum_of(node),
        }
        for name, node in sorted(properties.items())
    }


def _project_operation(operation: dict[str, Any]) -> dict[str, Any]:
    """Project one operation to its parameters, request body and response shapes."""
    parameters = sorted(
        f"{param.get('name')}:{param.get('in')}:"
        f"{'required' if param.get('required') else 'optional'}:"
        f"{_type_of(param.get('schema', {}))}"
        for param in operation.get("parameters", ())
    )
    body = operation.get("requestBody")
    request = (
        _type_of(body.get("content", {}).get("application/json", {}).get("schema", {}))
        if body
        else None
    )
    responses = {
        code: _type_of(
            spec.get("content", {}).get("application/json", {}).get("schema", {})
        )
        for code, spec in sorted(operation.get("responses", {}).items())
    }
    return {"params": parameters, "request": request, "responses": responses}


def build_contract(document: dict[str, Any]) -> dict[str, Any]:
    """Reduce a raw OpenAPI document to the projection that is actually gated."""
    paths = {
        f"{method.upper()} {path}": _project_operation(operation)
        for path, operations in sorted(document.get("paths", {}).items())
        for method, operation in sorted(operations.items())
        if method in _METHODS
    }
    schemas = {
        name: _project_schema(schema)
        for name, schema in sorted(
            document.get("components", {}).get("schemas", {}).items()
        )
    }
    return {
        "_generated_by": "backend/scripts/export_contract.py",
        "paths": paths,
        "schemas": schemas,
    }


def build_verdict_codes() -> dict[str, Any]:
    """The `VerdictCode` value set — the OpenAPI blind spot, gated separately."""
    return {
        "_generated_by": "backend/scripts/export_contract.py",
        "_why": (
            "Verdict.code is an open string on the wire, so VerdictCode never "
            "reaches the OpenAPI document. The SDK branches on 'budget_exhausted'."
        ),
        "codes": sorted(code.value for code in VerdictCode),
    }


def _dump(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def _check(path: Path, expected: str) -> bool:
    """True if `path` already holds `expected`; otherwise print a unified diff."""
    actual = path.read_text() if path.exists() else ""
    if actual == expected:
        return True
    diff = difflib.unified_diff(
        actual.splitlines(keepends=True),
        expected.splitlines(keepends=True),
        fromfile=f"{path.name} (committed)",
        tofile=f"{path.name} (generated)",
    )
    sys.stderr.write(f"\nCONTRACT DRIFT — {path.relative_to(_REPO_ROOT)}\n")
    sys.stderr.writelines(diff)
    return False


def main(argv: list[str] | None = None) -> int:
    """Regenerate the artifacts, or verify the gated two are current."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify the committed artifacts match the code; do not write",
    )
    args = parser.parse_args(argv)

    document = app.openapi()
    gated = {
        CONTRACT_PATH: _dump(build_contract(document)),
        VERDICT_CODES_PATH: _dump(build_verdict_codes()),
    }

    if args.check:
        # Check every artifact (no short-circuit) so all diffs print.
        current = True
        for path, text in gated.items():
            current = _check(path, text) and current
        if current:
            print("contract artifacts are current")
            return 0
        sys.stderr.write(
            "\nThe API contract changed. If that was intentional, regenerate with:\n"
            "  cd backend && python scripts/export_contract.py\n"
            "and review the diff as a contract change — the SDK and "
            "frontend/src/types/api.ts both read it.\n"
        )
        return 1

    CONTRACTS.mkdir(parents=True, exist_ok=True)
    for path, text in gated.items():
        path.write_text(text)
        print(f"wrote {path.relative_to(_REPO_ROOT)}")
    # Ungated: churns with dependency bumps; only feeds frontend type generation.
    OPENAPI_PATH.write_text(_dump(document))
    print(f"wrote {OPENAPI_PATH.relative_to(_REPO_ROOT)} (artifact, not gated)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
