#!/bin/bash

# Optional: installs a user service that runs tui_model.py through `textual serve`,
# exposing the TUI as a web app (default: http://localhost:8000).
# Requires: pip install textual textual-serve

set -e

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC_DIR="$REPO_ROOT/src"
LOG_DIR="$HOME/.local/share/aime-assistant"
VENV_DIR="$REPO_ROOT/.venv"
ENV_DIR="$HOME/.config/aime-assistant"
ENV_FILE="$ENV_DIR/env"
mkdir -p "$LOG_DIR"

TEXTUAL_BIN="$VENV_DIR/bin/textual"
PYTHON_BIN="$VENV_DIR/bin/python"

if [ ! -x "$TEXTUAL_BIN" ] || [ ! -x "$PYTHON_BIN" ]; then
    echo "Error: venv not set up at $VENV_DIR." >&2
    echo "Run ./install.sh first (it provisions the venv via uv)." >&2
    exit 1
fi

# Provision the API key env file (mode 600, outside the repo so it can never
# be committed). Reuse the existing key if one is already stored; otherwise
# prompt the user with no echo.
mkdir -p "$ENV_DIR"
chmod 700 "$ENV_DIR"

EXISTING_KEY=""
if [ -f "$ENV_FILE" ]; then
    # shellcheck disable=SC1090
    EXISTING_KEY="$(. "$ENV_FILE" >/dev/null 2>&1 && printf '%s' "${ANTHROPIC_API_KEY:-}")"
fi

if [ -n "$EXISTING_KEY" ]; then
    printf 'Found existing ANTHROPIC_API_KEY in %s. Replace it? [y/N] ' "$ENV_FILE"
    read -r REPLACE
    case "$REPLACE" in
        y|Y|yes|YES) EXISTING_KEY="" ;;
    esac
fi

if [ -z "$EXISTING_KEY" ]; then
    printf 'Enter your ANTHROPIC_API_KEY (input hidden): '
    stty -echo
    trap 'stty echo' EXIT INT TERM
    read -r NEW_KEY
    stty echo
    trap - EXIT INT TERM
    printf '\n'
    if [ -z "$NEW_KEY" ]; then
        echo "Error: empty API key." >&2
        exit 1
    fi
    umask 077
    # Write atomically so a partial write can't leak a half-quoted secret.
    TMP_ENV="$(mktemp "$ENV_DIR/.env.XXXXXX")"
    printf 'ANTHROPIC_API_KEY=%s\n' "$NEW_KEY" > "$TMP_ENV"
    chmod 600 "$TMP_ENV"
    mv "$TMP_ENV" "$ENV_FILE"
    unset NEW_KEY
    echo "Saved API key to $ENV_FILE (mode 600)."
fi
unset EXISTING_KEY

OS="$(uname -s)"

case "$OS" in
    Linux*)
        SYSTEMD_DIR="$HOME/.config/systemd/user"
        mkdir -p "$SYSTEMD_DIR"
        cat > "$SYSTEMD_DIR/aime-textual.service" <<EOF
[Unit]
Description=Aime textual web server
After=network.target aime-serve.service

[Service]
WorkingDirectory=$SRC_DIR
EnvironmentFile=$ENV_FILE
# Wrapped in sh -c so the inner quoted command stays a single argument to
# 'textual serve'. systemd's ExecStart= quote handling differs from a shell
# and will otherwise split "$PYTHON_BIN tui_model.py" on the space, leaving
# textual to try to read the python binary as a script.
ExecStart=/bin/sh -c '$TEXTUAL_BIN serve -c "$PYTHON_BIN tui_model.py"'
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
EOF
        systemctl --user daemon-reload
        systemctl --user enable aime-textual.service
        systemctl --user restart aime-textual.service
        echo "aime-textual installed and (re)started via systemd --user."
        echo "  open:    http://localhost:8000"
        echo "  status:  systemctl --user status aime-textual"
        echo "  logs:    journalctl --user -u aime-textual -f"
        ;;
    Darwin*)
        PLIST_DIR="$HOME/Library/LaunchAgents"
        mkdir -p "$PLIST_DIR"
        PLIST="$PLIST_DIR/com.aime.textual.plist"
        # launchd has no EnvironmentFile equivalent — pull the key out of the
        # secure env file and embed it into the plist, then chmod the plist
        # so it inherits the same restrictions as $ENV_FILE.
        # shellcheck disable=SC1090
        ANTHROPIC_API_KEY="$(. "$ENV_FILE" && printf '%s' "$ANTHROPIC_API_KEY")"
        umask 077
        cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.aime.textual</string>
    <key>ProgramArguments</key>
    <array>
        <string>$TEXTUAL_BIN</string>
        <string>serve</string>
        <string>tui_model.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$SRC_DIR</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$LOG_DIR/aime-textual.log</string>
    <key>StandardErrorPath</key>
    <string>$LOG_DIR/aime-textual.err</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>HOME</key>
        <string>$HOME</string>
        <key>PATH</key>
        <string>$(dirname "$TEXTUAL_BIN"):/usr/local/bin:/usr/bin:/bin</string>
        <key>ANTHROPIC_API_KEY</key>
        <string>$ANTHROPIC_API_KEY</string>
    </dict>
</dict>
</plist>
EOF
        chmod 600 "$PLIST"
        unset ANTHROPIC_API_KEY
        launchctl unload "$PLIST" 2>/dev/null || true
        launchctl load -w "$PLIST"
        echo "aime-textual installed and (re)started via launchd."
        echo "  open:    http://localhost:8000"
        echo "  status:  launchctl list | grep com.aime.textual"
        echo "  logs:    tail -f $LOG_DIR/aime-textual.log"
        ;;
    *)
        echo "Unsupported OS: $OS" >&2
        exit 1
        ;;
esac
