#!/bin/bash

# Runs the usage-log analysis web page (frontends/usage_dashboard.py) directly
# in the foreground — no systemd/launchd service, unlike web_app_serve.sh.
# Ctrl-C to stop. Reads <database>/usage/usage.jsonl; collects nothing itself.
#
#   AIME_USAGE_DASHBOARD_PORT   listen port (default 5050)
#   AIME_DATABASE_DIR           usage.jsonl location (default: app data dir)

set -e

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC_DIR="$REPO_ROOT/src"
VENV_DIR="$REPO_ROOT/.venv"
PYTHON_BIN="$VENV_DIR/bin/python"

if [ ! -x "$PYTHON_BIN" ]; then
    echo "Error: venv not set up at $VENV_DIR." >&2
    echo "Run ./install.sh first (it provisions the venv via uv)." >&2
    exit 1
fi

cd "$SRC_DIR"
exec "$PYTHON_BIN" -m frontends.usage_dashboard
