#!/bin/bash

# Optional: installs a user service that runs the Flask web frontend
# (frontends/web_app.py), exposing Aime as a web app at http://localhost:5000.
# Requires the venv set up by ./install.sh (flask is in requirements.txt).

set -e

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC_DIR="$REPO_ROOT/src"
LOG_DIR="$HOME/.local/share/aime-assistant"
VENV_DIR="$REPO_ROOT/.venv"
ENV_DIR="$HOME/.config/aime-assistant"
ENV_FILE="$ENV_DIR/env"
mkdir -p "$LOG_DIR"

PYTHON_BIN="$VENV_DIR/bin/python"

if [ ! -x "$PYTHON_BIN" ]; then
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
    TMP_ENV="$(mktemp "$ENV_DIR/.env.XXXXXX")"
    printf 'ANTHROPIC_API_KEY=%s\n' "$NEW_KEY" > "$TMP_ENV"
    chmod 600 "$TMP_ENV"
    mv "$TMP_ENV" "$ENV_FILE"
    unset NEW_KEY
    echo "Saved API key to $ENV_FILE (mode 600)."
fi
unset EXISTING_KEY

# Web server configuration. Prompt the user for each value; pressing Enter
# accepts the default shown in the prompt.
printf 'Serve over HTTPS? Enter AIME_HTTPS value (0 = plain HTTP, 1 = HTTPS; enter to use default of 1): '
read -r AIME_HTTPS
AIME_HTTPS="${AIME_HTTPS:-1}"

printf 'Enter AIME_BIND value (enter to use default of 127.0.0.1): '
read -r AIME_BIND
AIME_BIND="${AIME_BIND:-127.0.0.1}"

echo "Using AIME_HTTPS=$AIME_HTTPS, AIME_BIND=$AIME_BIND"

# Account creation gate. Default 0 (disabled): the admin creates accounts, then
# runs the server closed so nobody else can register. Set to 1 to allow public
# signup.
printf 'Allow new account creation? Enter AIME_ALLOW_SIGNUP value (0 = no, 1 = yes; enter to use default of 0): '
read -r AIME_ALLOW_SIGNUP
AIME_ALLOW_SIGNUP="${AIME_ALLOW_SIGNUP:-0}"

echo "Using AIME_ALLOW_SIGNUP=$AIME_ALLOW_SIGNUP"

# Usage statistics. Default 0 (disabled): nothing is collected. When enabled,
# per-call API token usage and local speech-to-text compute are appended to
# <database>/usage/usage.jsonl. A second toggle decides whether each record is
# tagged with the username (1) or kept anonymous (0).
printf 'Collect usage statistics? Enter AIME_USAGE_STATS value (0 = no, 1 = yes; enter to use default of 0): '
read -r AIME_USAGE_STATS
AIME_USAGE_STATS="${AIME_USAGE_STATS:-0}"

AIME_USAGE_LINK_USERS=0
if [ "$AIME_USAGE_STATS" = "1" ]; then
    printf 'Link usage statistics to usernames? Enter AIME_USAGE_LINK_USERS value (0 = anonymous, 1 = tag with username; enter to use default of 0): '
    read -r AIME_USAGE_LINK_USERS
    AIME_USAGE_LINK_USERS="${AIME_USAGE_LINK_USERS:-0}"
fi

echo "Using AIME_USAGE_STATS=$AIME_USAGE_STATS, AIME_USAGE_LINK_USERS=$AIME_USAGE_LINK_USERS"

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
        <key>AIME_USAGE_STATS</key>
        <string>$AIME_USAGE_STATS</string>
        <key>AIME_USAGE_LINK_USERS</key>
        <string>$AIME_USAGE_LINK_USERS</string>
    </dict>
</dict>
</plist>
EOF
        chmod 600 "$PLIST"
        unset ANTHROPIC_API_KEY
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
