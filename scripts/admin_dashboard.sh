#!/bin/bash

# Runs the admin dashboard (frontends/usage_dashboard.py) directly in the
# foreground — no systemd/launchd service, unlike web_app_serve.sh. Ctrl-C to
# stop. Shows usage statistics and manages accounts and invite keys.
#
# Configuration is read from the repo .env file (the same file the Docker path
# and web_app_serve.sh use):
#
#   AIME_ADMIN_PASSWORD         REQUIRED — password gate; the app exits without it
#   AIME_USAGE_DASHBOARD_PORT   listen port (default 5050)
#   AIME_DATABASE_DIR           data location (usage.jsonl, auth.sql)

set -e

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC_DIR="$REPO_ROOT/src"
VENV_DIR="$REPO_ROOT/.venv"
PYTHON_BIN="$VENV_DIR/bin/python"
ENV_FILE="$REPO_ROOT/.env"

if [ ! -x "$PYTHON_BIN" ]; then
    echo "Error: venv not set up at $VENV_DIR." >&2
    echo "Run ./install.sh first (it provisions the venv via uv)." >&2
    exit 1
fi

# Load configuration from the repo .env file so AIME_ADMIN_PASSWORD and the
# other settings are available — running the module directly does not pick
# up .env on its own.
if [ -f "$ENV_FILE" ]; then
    # shellcheck disable=SC1090
    set -a
    . "$ENV_FILE"
    set +a
fi

if [ -z "${AIME_ADMIN_PASSWORD:-}" ]; then
    echo "Error: AIME_ADMIN_PASSWORD is empty." >&2
    echo "Set it in $ENV_FILE — the admin dashboard requires a password." >&2
    exit 1
fi

cd "$SRC_DIR"
exec "$PYTHON_BIN" -m frontends.usage_dashboard
