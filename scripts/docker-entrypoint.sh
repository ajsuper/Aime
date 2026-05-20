#!/bin/bash
# Container entrypoint for Aime.
#
# Replaces the interactive prompts of web_app_serve.sh / backend_serve.sh with
# plain environment variables. Starts the C++ backend, waits for it to accept
# connections, then runs the Flask web app in the foreground (PID 1) so Docker
# can manage its lifecycle.
set -e

REPO_ROOT="/app"
SERVE_BIN="$REPO_ROOT/build/serve.o"

if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
    echo "Error: ANTHROPIC_API_KEY is not set." >&2
    echo "Pass it with 'docker run -e ANTHROPIC_API_KEY=...' or via the .env file." >&2
    exit 1
fi

# Persistent data lives under $HOME (=/data, a volume). aime/config.py derives
# every path from it; ensure the tree exists before either process starts.
mkdir -p "$HOME/.local/share/aime-assistant/database" \
         "$HOME/.local/share/aime-assistant/conversations" \
         "$HOME/.config/aime-assistant"

# Start the C++ backend (binds 127.0.0.1:8080 inside this container).
"$SERVE_BIN" &
BACKEND_PID=$!

# Stop the backend cleanly when the container is told to shut down.
trap 'kill "$BACKEND_PID" 2>/dev/null || true' TERM INT

# Wait for the backend to accept connections before launching the web app,
# which expects http://localhost:8080 to be live.
echo "Waiting for the backend on 127.0.0.1:8080 ..."
for _ in $(seq 1 30); do
    if python -c 'import socket,sys; s=socket.socket(); s.settimeout(1); sys.exit(0 if s.connect_ex(("127.0.0.1",8080))==0 else 1)' 2>/dev/null; then
        echo "Backend is up."
        break
    fi
    if ! kill -0 "$BACKEND_PID" 2>/dev/null; then
        echo "Error: backend exited before becoming ready." >&2
        exit 1
    fi
    sleep 1
done

# The web app and usage dashboard read files via relative paths, so the
# working directory must be src/.
cd "$REPO_ROOT/src"

# Optionally start the usage dashboard (frontends/usage_dashboard.py) when
# AIME_USAGE_DASHBOARD=1. It binds 0.0.0.0 so the mapped Docker port works;
# the dashboard can expose usernames, so only map its port on a trusted host.
if [ "${AIME_USAGE_DASHBOARD:-0}" = "1" ]; then
    echo "Starting usage dashboard on port ${AIME_USAGE_DASHBOARD_PORT:-5050} ..."
    AIME_USAGE_DASHBOARD_HOST="${AIME_USAGE_DASHBOARD_HOST:-0.0.0.0}" \
        python -m frontends.usage_dashboard &
    DASHBOARD_PID=$!
    trap 'kill "$BACKEND_PID" "$DASHBOARD_PID" 2>/dev/null || true' TERM INT
fi

# Default the web app bind to 0.0.0.0 so the mapped Docker port is reachable
# (the host scripts default to 127.0.0.1).
export AIME_BIND="${AIME_BIND:-0.0.0.0}"
exec python -m frontends.web_app
