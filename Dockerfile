# Aime — containerized run path.
#
# This is an OPTIONAL alternative to the native install (scripts/install.sh +
# the systemd/launchd services). It changes nothing about that flow; it just
# packages the same C++ backend + Flask web app into one image.
#
# Single container by design: the C++ backend binds 127.0.0.1:8080 and the web
# app talks to http://localhost:8080, both hardcoded. Running them in one
# container keeps those assumptions valid with zero source changes.

# ---------------------------------------------------------------------------
# Stage 1 — compile the C++ backend (src/serve.cpp -> build/serve.o)
# ---------------------------------------------------------------------------
FROM debian:bookworm-slim AS backend-build

RUN apt-get update && apt-get install -y --no-install-recommends \
        g++ \
        libsqlite3-dev \
        libasio-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY src/serve.cpp src/serve.cpp
COPY include/crow_all.h include/crow_all.h

# Mirrors scripts/install.sh's compile line. The crow header is pulled in via
# the source file's relative "../include/crow_all.h" include.
RUN g++ -std=c++17 -O2 src/serve.cpp -lsqlite3 -o serve.o

# ---------------------------------------------------------------------------
# Stage 2 — Python runtime
# ---------------------------------------------------------------------------
FROM python:3.13-slim AS runtime

# Runtime libs: sqlite3 shared lib for the backend, certs for HTTPS/API calls.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libsqlite3-0 \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# uv, the same installer scripts/install.sh uses on the host.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Install Python deps first so the layer caches across source changes.
COPY requirements.txt .
RUN uv pip install --system --no-cache -r requirements.txt

# Application code, resources, and the compiled backend.
COPY src ./src
COPY resources ./resources
COPY --from=backend-build /build/serve.o ./build/serve.o

# The usage dashboard imports scripts/usage_report.py (it adds <repo>/scripts
# to sys.path). Copy it in so the dashboard can start; without it the
# dashboard process crashes on import.
COPY scripts/usage_report.py ./scripts/usage_report.py

# Pre-download the practical, commonly-used Whisper STT models (tiny / base /
# small, ~700MB total) so voice input is instant out of the box. The heavier
# models (medium, large-v3, large-v3-turbo) still work — faster-whisper just
# fetches them on first use into the mounted models volume.
#
# frontends/stt.py resolves the model dir as <repo>/models/whisper, which is
# /app/models/whisper here, and reads the default UI choice from .selected.
ARG WHISPER_MODEL=small.en
RUN mkdir -p /app/models/whisper && \
    python - /app/models/whisper <<'PY'
import sys
from faster_whisper import WhisperModel
cache = sys.argv[1]
for name in ["tiny.en", "base.en", "small.en"]:
    print(f"Downloading Whisper model {name!r} ...", flush=True)
    WhisperModel(name, device="cpu", compute_type="int8", download_root=cache)
print(f"Whisper models cached under {cache}")
PY

RUN echo "$WHISPER_MODEL" > /app/models/whisper/.selected

# HOME drives every data path: aime/config.py derives DATABASE_DIR, the
# conversations dir, and CONFIG_PATH from it. Point it at /data so a single
# volume captures all persistent state (databases, conversations, encryption
# keys, auth, TLS cert).
ENV HOME=/data
RUN mkdir -p /data && chmod 700 /data

COPY scripts/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

EXPOSE 5000
VOLUME ["/data"]

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
