#!/bin/bash
# Run this script whenever you make changes to source.
# Compiles the backend binary and sets up config/data directories.
# To run the backend as a service, see backend_serve.sh.
set -e

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG_DIR="$HOME/.config/aime-assistant"
DATABASE_DIR="$HOME/.local/share/aime-assistant/database"
LOG_DIR="$HOME/.local/share/aime-assistant"
VENV_DIR="$REPO_ROOT/.venv"

mkdir -p "$CONFIG_DIR"
mkdir -p "$REPO_ROOT/build"
mkdir -p "$DATABASE_DIR"
mkdir -p "$LOG_DIR"

# Copies all default configuration files to the specified config dir.
cp "$REPO_ROOT"/resources/default-config/.* "$CONFIG_DIR"/ 2>/dev/null || true

# Compile the c++ binary
g++ "$REPO_ROOT/src/serve.cpp" -lsqlite3 -o "$REPO_ROOT/build/serve.o"

# Set up the python venv used by tui_model.py / textual_serve.sh on the host
# via uv. We pin to a uv-managed standalone interpreter (--managed-python)
# so the venv survives system python upgrades (e.g. rpm-ostree bumping
# /usr/bin/python from 3.13 to 3.14) that would otherwise leave .venv/bin/python
# pointing at a mismatched interpreter and hide every installed package.
if ! command -v uv >/dev/null 2>&1; then
    echo "Error: uv not found in PATH." >&2
    echo "Install it with one of:" >&2
    echo "  Standalone:      curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
    echo "  Fedora/Bazzite:  sudo rpm-ostree install uv  (or 'dnf install' in a toolbox)" >&2
    echo "  Homebrew (mac):  brew install uv" >&2
    exit 1
fi

PYTHON_VERSION="3.13"

# Rebuild the venv if it exists but its interpreter is broken (e.g. the system
# python it linked to has been upgraded out from under it). `uv venv` won't
# replace a venv whose pyvenv.cfg looks valid even if `bin/python` no longer
# runs, so probe with `--version` and nuke on failure.
if [ -d "$VENV_DIR" ] && ! "$VENV_DIR/bin/python" --version >/dev/null 2>&1; then
    echo "Existing $VENV_DIR has a broken interpreter; rebuilding."
    rm -rf "$VENV_DIR"
fi

# Ensure uv has a managed copy of the requested Python before building the
# venv. Required when uv's python-downloads policy is "manual" (the default
# for distro-packaged uv); a no-op if already installed.
uv python install "$PYTHON_VERSION"

uv venv --managed-python --python "$PYTHON_VERSION" "$VENV_DIR"
uv pip install --python "$VENV_DIR/bin/python" -r "$REPO_ROOT/requirements.txt"

echo "aime build complete."
echo "  binary:  $REPO_ROOT/build/serve.o"
echo "  venv:    $VENV_DIR"
echo "  to run as a background service: ./backend_serve.sh"
