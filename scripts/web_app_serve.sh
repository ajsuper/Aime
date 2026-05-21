#!/bin/bash

# Optional: installs a user service that runs the Flask web frontend
# (frontends/web_app.py), exposing Aime as a web app at http://localhost:5000.
# Requires the venv set up by ./install.sh (flask is in requirements.txt).
#
# Configuration is read entirely from the repo .env file (the same file the
# Docker path uses). Copy .env.example to .env and fill it in first; this
# script no longer prompts for anything.

set -e

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC_DIR="$REPO_ROOT/src"
LOG_DIR="$HOME/.local/share/aime-assistant"
VENV_DIR="$REPO_ROOT/.venv"
ENV_FILE="$REPO_ROOT/.env"
mkdir -p "$LOG_DIR"

PYTHON_BIN="$VENV_DIR/bin/python"

if [ ! -x "$PYTHON_BIN" ]; then
    echo "Error: venv not set up at $VENV_DIR." >&2
    echo "Run ./install.sh first (it provisions the venv via uv)." >&2
    exit 1
fi

# Load configuration from the repo .env file. This is the single source of
# truth for both this native install path and the Docker path.
if [ ! -f "$ENV_FILE" ]; then
    echo "Error: $ENV_FILE not found." >&2
    echo "Copy .env.example to .env and fill it in first." >&2
    exit 1
fi
# shellcheck disable=SC1090
set -a
. "$ENV_FILE"
set +a

# Required key.
if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
    echo "Error: ANTHROPIC_API_KEY is empty in $ENV_FILE." >&2
    exit 1
fi

# Web server configuration. Apply the documented defaults for anything the
# .env file leaves unset.
AIME_HTTPS="${AIME_HTTPS:-1}"
AIME_BIND="${AIME_BIND:-127.0.0.1}"
AIME_ALLOW_SIGNUP="${AIME_ALLOW_SIGNUP:-0}"
AIME_ACCESS_MODE="${AIME_ACCESS_MODE:-keys}"
case "$AIME_ACCESS_MODE" in
    open|keys) ;;
    *) echo "Unrecognised AIME_ACCESS_MODE '$AIME_ACCESS_MODE'; using 'open'." >&2
       AIME_ACCESS_MODE="open" ;;
esac
AIME_USAGE_STATS="${AIME_USAGE_STATS:-0}"
AIME_USAGE_LINK_USERS="${AIME_USAGE_LINK_USERS:-0}"
if [ "$AIME_USAGE_STATS" != "1" ]; then
    AIME_USAGE_LINK_USERS=0
fi

echo "Using AIME_HTTPS=$AIME_HTTPS, AIME_BIND=$AIME_BIND"
echo "Using AIME_ALLOW_SIGNUP=$AIME_ALLOW_SIGNUP, AIME_ACCESS_MODE=$AIME_ACCESS_MODE"
echo "Using AIME_USAGE_STATS=$AIME_USAGE_STATS, AIME_USAGE_LINK_USERS=$AIME_USAGE_LINK_USERS"

# Bill-bomb guard: reachable off-host + open signup + no send-gate means any
# stranger can register and immediately spend the Anthropic API budget.
if [ "$AIME_BIND" != "127.0.0.1" ] && [ "$AIME_ALLOW_SIGNUP" = "1" ] \
   && [ "$AIME_ACCESS_MODE" = "open" ]; then
    echo "" >&2
    echo "WARNING: AIME_BIND=$AIME_BIND + AIME_ALLOW_SIGNUP=1 + AIME_ACCESS_MODE=open" >&2
    echo "  lets anyone who can reach this server create an account and" >&2
    echo "  immediately spend your Anthropic API budget. For a public" >&2
    echo "  deployment set AIME_ACCESS_MODE=keys. See docs/access-control.md." >&2
fi

OS="$(uname -s)"

case "$OS" in
    Linux*)
        SYSTEMD_DIR="$HOME/.config/systemd/user"
        mkdir -p "$SYSTEMD_DIR"
        cat > "$SYSTEMD_DIR/aime-webapp.service" <<EOF
[Unit]
Description=Aime Flask web frontend
After=network.target aime-serve.service

[Service]
WorkingDirectory=$SRC_DIR
EnvironmentFile=$ENV_FILE
Environment=AIME_HTTPS=$AIME_HTTPS
Environment=AIME_BIND=$AIME_BIND
Environment=AIME_ALLOW_SIGNUP=$AIME_ALLOW_SIGNUP
Environment=AIME_ACCESS_MODE=$AIME_ACCESS_MODE
Environment=AIME_USAGE_STATS=$AIME_USAGE_STATS
Environment=AIME_USAGE_LINK_USERS=$AIME_USAGE_LINK_USERS
ExecStart=$PYTHON_BIN -m frontends.web_app
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
EOF
        systemctl --user daemon-reload
        systemctl --user enable aime-webapp.service
        systemctl --user restart aime-webapp.service
        echo "aime-webapp installed and (re)started via systemd --user."
        echo "  open:    http://localhost:5000"
        echo "  status:  systemctl --user status aime-webapp"
        echo "  logs:    journalctl --user -u aime-webapp -f"
        ;;
    Darwin*)
        PLIST_DIR="$HOME/Library/LaunchAgents"
        mkdir -p "$PLIST_DIR"
        PLIST="$PLIST_DIR/com.aime.webapp.plist"
        # launchd has no EnvironmentFile equivalent — embed the values pulled
        # from .env into the plist, then chmod it so the API key is protected.
        umask 077
        cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.aime.webapp</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON_BIN</string>
        <string>-m</string>
        <string>frontends.web_app</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$SRC_DIR</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$LOG_DIR/aime-webapp.log</string>
    <key>StandardErrorPath</key>
    <string>$LOG_DIR/aime-webapp.err</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>HOME</key>
        <string>$HOME</string>
        <key>PATH</key>
        <string>$(dirname "$PYTHON_BIN"):/usr/local/bin:/usr/bin:/bin</string>
        <key>ANTHROPIC_API_KEY</key>
        <string>$ANTHROPIC_API_KEY</string>
        <key>AIME_HTTPS</key>
        <string>$AIME_HTTPS</string>
        <key>AIME_BIND</key>
        <string>$AIME_BIND</string>
        <key>AIME_ALLOW_SIGNUP</key>
        <string>$AIME_ALLOW_SIGNUP</string>
        <key>AIME_ACCESS_MODE</key>
        <string>$AIME_ACCESS_MODE</string>
        <key>AIME_USAGE_STATS</key>
        <string>$AIME_USAGE_STATS</string>
        <key>AIME_USAGE_LINK_USERS</key>
        <string>$AIME_USAGE_LINK_USERS</string>
    </dict>
</dict>
</plist>
EOF
        chmod 600 "$PLIST"
        launchctl unload "$PLIST" 2>/dev/null || true
        launchctl load -w "$PLIST"
        echo "aime-webapp installed and (re)started via launchd."
        echo "  open:    http://localhost:5000"
        echo "  status:  launchctl list | grep com.aime.webapp"
        echo "  logs:    tail -f $LOG_DIR/aime-webapp.log"
        ;;
    *)
        echo "Unsupported OS: $OS" >&2
        exit 1
        ;;
esac
