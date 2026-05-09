#!/bin/bash

# Installs a user service that runs the Aime backend binary continuously.
# Requires: install.sh to have been run first (so the binary exists).

set -e

REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="$HOME/.local/share/aime-assistant"
SERVE_BIN="$REPO_ROOT/build/serve.o"

if [ ! -x "$SERVE_BIN" ]; then
    echo "Error: backend binary not found at $SERVE_BIN" >&2
    echo "Run ./install.sh first to compile the backend." >&2
    exit 1
fi

mkdir -p "$LOG_DIR"

OS="$(uname -s)"

case "$OS" in
    Linux*)
        SYSTEMD_DIR="$HOME/.config/systemd/user"
        mkdir -p "$SYSTEMD_DIR"
        cat > "$SYSTEMD_DIR/aime-serve.service" <<EOF
[Unit]
Description=Aime backend server
After=network.target

[Service]
ExecStart=$SERVE_BIN
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
EOF
        systemctl --user daemon-reload
        systemctl --user enable aime-serve.service
        systemctl --user restart aime-serve.service
        echo "aime-serve installed and (re)started via systemd --user."
        echo "  status:  systemctl --user status aime-serve"
        echo "  logs:    journalctl --user -u aime-serve -f"
        ;;
    Darwin*)
        PLIST_DIR="$HOME/Library/LaunchAgents"
        mkdir -p "$PLIST_DIR"
        PLIST="$PLIST_DIR/com.aime.serve.plist"
        cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.aime.serve</string>
    <key>ProgramArguments</key>
    <array>
        <string>$SERVE_BIN</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$LOG_DIR/aime-serve.log</string>
    <key>StandardErrorPath</key>
    <string>$LOG_DIR/aime-serve.err</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>HOME</key>
        <string>$HOME</string>
    </dict>
</dict>
</plist>
EOF
        launchctl unload "$PLIST" 2>/dev/null || true
        launchctl load -w "$PLIST"
        echo "aime-serve installed and (re)started via launchd."
        echo "  status:  launchctl list | grep com.aime.serve"
        echo "  logs:    tail -f $LOG_DIR/aime-serve.log"
        ;;
    *)
        echo "Unsupported OS: $OS" >&2
        exit 1
        ;;
esac
