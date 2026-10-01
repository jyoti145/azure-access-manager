#!/usr/bin/env bash
# Azure Access Manager - one-click start for Linux / macOS.
# First run: creates .venv and installs dependencies. Then opens the UI in your browser.
set -euo pipefail
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"
if [ ! -x ".venv/bin/python" ]; then
    echo "Creating Python virtual environment..."
    if ! "$PYTHON" -m venv .venv; then
        echo "Python 3.11+ with the venv module is required (e.g. sudo apt install python3-venv)." >&2
        exit 1
    fi
fi

if ! .venv/bin/python -c "import streamlit, azure.identity, azure.mgmt.authorization, yaml" 2>/dev/null; then
    echo "Installing dependencies - this takes a minute the first time..."
    .venv/bin/python -m pip --version >/dev/null 2>&1 || .venv/bin/python -m ensurepip --upgrade
    .venv/bin/python -m pip install --disable-pip-version-check -q -r requirements.txt
fi

exec .venv/bin/python azure_manager.py ui "$@"
