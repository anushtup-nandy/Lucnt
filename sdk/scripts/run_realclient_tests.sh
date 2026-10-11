#!/usr/bin/env bash
# Acceptance: run tests-realclient/ on the built wheel with REAL vendor clients
# against a local fake server (no API keys, no money).
set -euo pipefail
cd "$(dirname "$0")/.."

VENV=".venv-realclient"

rm -rf dist "$VENV"
uv build
python3 -m venv "$VENV"
"$VENV/bin/pip" -q install --upgrade pip
# openai/langchain-openai pinned to a known with_raw_response path; anthropic floats.
"$VENV/bin/pip" -q install dist/*.whl pytest "openai==2.49.0" "langchain-openai==1.4.1" anthropic

# Empty pythonpath so the installed wheel is imported, not src/.
"$VENV/bin/pytest" tests-realclient/ -o pythonpath= -p no:cacheprovider "$@"
