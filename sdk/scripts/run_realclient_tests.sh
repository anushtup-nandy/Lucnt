#!/usr/bin/env bash
# Acceptance: run the SDK against the REAL vendor client libraries.
#
# Builds the wheel, installs it with pinned real `openai`/`anthropic`/
# `langchain-openai` into a throwaway venv, and runs tests-realclient/ against
# a local fake vendor+lucnt server (no real API keys, no real money — real
# client libraries, fake endpoints). This is the class of test the unit suite
# cannot be: its fakes only expose the paths the adapters name.
set -euo pipefail
cd "$(dirname "$0")/.."

VENV=".venv-realclient"

rm -rf dist "$VENV"
uv build
python3 -m venv "$VENV"
"$VENV/bin/pip" -q install --upgrade pip
# Pins: the exact versions the original investigation traced (langchain-openai's
# with_raw_response path); anthropic floats to catch its current wire shapes.
"$VENV/bin/pip" -q install dist/*.whl pytest "openai==2.49.0" "langchain-openai==1.4.1" anthropic

# -o pythonpath= drops the repo's `src` injection so the INSTALLED wheel is
# what gets imported, not the working tree.
"$VENV/bin/pytest" tests-realclient/ -o pythonpath= -p no:cacheprovider "$@"
