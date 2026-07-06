"""Authoritative Vega-Lite validation for CreateGraphics.

`graphics.validate` is a *cheap* gate — it confirms a Vega-Lite `source` is valid
JSON with a spec-shaped top level, but it can't tell a well-formed spec from a
structurally-broken one (a bad channel, a malformed `layer`, an `errorband`
missing its `extent`). Those are exactly the specs the model gets wrong on the
harder charts, and today they pass validation, get "Saved as …", and then fail
*silently* in the browser (a client-side "Couldn't render" card the model never
sees) — so no retry ever fires.

This module closes that loop. It runs the same two steps the frontend runs before
it can draw — vega-lite `compile` → vega `parse` (see resources/vega/compile.mjs
and web_chat.html `ensureVega`) — in a short-lived Node subprocess pinned to the
same major the browser loads. A spec that won't compile comes back with the real
reason, which the controller hands to the model for a same-turn fix.

Graceful by design: if Node or the pinned deps aren't present (a bare prod box),
or the compile times out or misbehaves, `compile_error` returns None — "no
objection" — so CreateGraphics falls back to today's loose gate rather than
blocking on our own infrastructure. It only ever returns a non-None string when
the compiler *definitively* rejected the spec.
"""

import json
import os
import shutil
import subprocess

# Repo root holds package.json + node_modules and resources/vega/compile.mjs.
# Resolve from this file so it works regardless of the process's cwd (config.py's
# relative paths assume cwd=src/, which we don't want to depend on here).
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_COMPILE_SCRIPT = os.path.join(_REPO_ROOT, "resources", "vega", "compile.mjs")
_NODE_MODULES = os.path.join(_REPO_ROOT, "node_modules")

# A spec larger than this we don't bother compiling (a pathological multi-thousand
# line source): skip the subprocess and let the loose gate handle it. The strip
# keeps such a source out of context after the turn anyway.
_MAX_SOURCE_CHARS = 200_000


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


# Node cold-start + importing vega is a few hundred ms; give it generous headroom
# so a slow box doesn't spuriously "fail open". AIME_VEGA_COMPILE_TIMEOUT tunes it;
# 0 disables the gate entirely (falls back to the loose check).
_TIMEOUT_S = _env_float("AIME_VEGA_COMPILE_TIMEOUT", 6.0)


def available() -> bool:
    """Whether the Node compile gate can run: node on PATH, the script present,
    and the pinned deps installed. Cheap stat calls; called per compile so a box
    that installs deps mid-run picks them up without a restart."""
    if _TIMEOUT_S <= 0:
        return False
    return bool(
        shutil.which("node")
        and os.path.isfile(_COMPILE_SCRIPT)
        and os.path.isdir(_NODE_MODULES)
    )


def compile_error(source: str) -> str | None:
    """Return the compiler's reason a Vega-Lite `source` won't render, or None if
    it compiles cleanly *or* the gate is unavailable/misbehaving. A non-None
    result is a definitive rejection safe to hand back to the model; None means
    "no objection" so the caller proceeds on the loose gate alone.

    `source` must already be JSON-valid (graphics.validate runs first)."""
    if not isinstance(source, str) or not source.strip():
        return None
    if len(source) > _MAX_SOURCE_CHARS:
        return None
    if not available():
        return None

    node = shutil.which("node")
    try:
        proc = subprocess.run(
            [node, "--no-deprecation", _COMPILE_SCRIPT],
            input=source,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_S,
            cwd=_REPO_ROOT,  # so Node resolves node_modules from the repo root
        )
    except (subprocess.TimeoutExpired, OSError):
        # Timed out or couldn't spawn — treat as unavailable, don't block a valid
        # spec on our own hiccup.
        return None

    line = (proc.stdout or "").strip().splitlines()
    if not line:
        return None
    try:
        result = json.loads(line[-1])
    except ValueError:
        return None
    if not isinstance(result, dict):
        return None

    # ok: True  → compiles; ok: False → invalid (return the reason);
    # ok: null / anything else → internal harness error, fail open.
    if result.get("ok") is False:
        err = result.get("error")
        return err.strip() if isinstance(err, str) and err.strip() else (
            "the chart spec didn't compile"
        )
    return None
