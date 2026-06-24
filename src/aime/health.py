"""Service-health snapshot for the public ``/health`` page.

Aime's availability has two moving parts, and this module folds them into one
at-a-glance status:

* **Anthropic API** — Aime is only as up as the model provider behind it, so we
  read Anthropic's public status page (the standard Statuspage.io JSON feed) and
  surface its indicator. The result is cached for a minute so a burst of page
  hits doesn't hammer the provider.
* **Aime service** — derived from :class:`aime.errors.ErrorStore`: a quiet error
  log means things are healthy; a recent run of *unexpected* (``unknown``)
  errors downgrades the service to "degraded", a flood to a "disruption".
  Provider blips (``transient``) and bad client input (``client``) deliberately
  don't move this dial — the former shows up under the Anthropic component, the
  latter isn't an outage.

Everything here is best-effort and never raises. The health page has to stay up
precisely when other things are falling over, so a failed provider fetch or an
unreadable error store degrades to an honest "unknown" rather than a 500.
"""

from __future__ import annotations

import datetime
import threading
import time

import requests


# Anthropic's public status feed (Statuspage.io). ``indicator`` is one of
# none/minor/major/critical; ``description`` is the human summary we echo.
_ANTHROPIC_STATUS_URL = "https://status.anthropic.com/api/v2/status.json"
_FETCH_TIMEOUT = 4.0          # seconds — keep the page snappy even when down
_CACHE_TTL = 60.0             # seconds — don't poll the provider per request

# Our four-state vocabulary, ordered worst-last so the overall status is just
# the max across components. "unknown" sits above "operational" so a component
# we can't read never lets us claim everything is fine.
STATUSES = ("operational", "unknown", "degraded", "outage")
_ORDER = {s: i for i, s in enumerate(STATUSES)}

_OVERALL_LABEL = {
    "operational": "All systems operational",
    "degraded": "Some systems degraded",
    "outage": "Service disruption",
    "unknown": "Status unknown",
}

# Statuspage indicator -> our vocabulary.
_ANTHROPIC_MAP = {
    "none": "operational",
    "minor": "degraded",
    "major": "outage",
    "critical": "outage",
    "maintenance": "degraded",
}

# Unexpected-error thresholds (last hour) that move the Aime-service dial.
_DEGRADED_AT = 1     # any unexpected error in the last hour -> degraded
_OUTAGE_AT = 25      # a sustained flood -> call it a disruption


def _worst(statuses: list[str]) -> str:
    """The most severe status in the list (our overall roll-up)."""
    return max(statuses, key=lambda s: _ORDER.get(s, _ORDER["unknown"]))


# ---------------------------------------------------------------------------
# Anthropic component (cached provider fetch)
# ---------------------------------------------------------------------------

_cache_lock = threading.Lock()
_cache: dict = {"at": 0.0, "value": None}


def _fetch_anthropic() -> dict | None:
    """Fetch + parse Anthropic's status feed, or ``None`` on any failure."""
    try:
        resp = requests.get(_ANTHROPIC_STATUS_URL, timeout=_FETCH_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return None
    status = data.get("status") or {}
    indicator = str(status.get("indicator") or "").lower()
    description = str(status.get("description") or "").strip()
    return {"indicator": indicator, "description": description}


def anthropic_component() -> dict:
    """The Anthropic-API component, served from a short-lived cache."""
    now = time.monotonic()
    with _cache_lock:
        cached = _cache["value"]
        fresh = cached is not None and (now - _cache["at"]) < _CACHE_TTL
    if not fresh:
        fetched = _fetch_anthropic()
        with _cache_lock:
            # Keep the last good reading if this fetch failed, so a momentary
            # blip talking to the provider doesn't flip the page to "unknown".
            if fetched is not None:
                _cache["value"] = fetched
                _cache["at"] = now
                cached = fetched
            else:
                cached = _cache["value"]

    if cached is None:
        return {
            "id": "anthropic", "name": "Anthropic API", "status": "unknown",
            "detail": "Couldn't reach the provider status page.",
        }
    status = _ANTHROPIC_MAP.get(cached["indicator"], "unknown")
    detail = cached["description"] or {
        "operational": "All systems operational",
        "degraded": "Degraded performance",
        "outage": "Major outage",
        "unknown": "Status unavailable",
    }[status]
    return {"id": "anthropic", "name": "Anthropic API",
            "status": status, "detail": detail}


# ---------------------------------------------------------------------------
# Aime component (derived from the error store)
# ---------------------------------------------------------------------------

def aime_component(error_store) -> dict:
    """The Aime-service component, derived from recent *unexpected* errors.

    ``error_store`` is an :class:`aime.errors.ErrorStore` (or anything with the
    same ``recent`` shape). Passing ``None`` yields an honest "unknown".
    """
    if error_store is None:
        return {"id": "aime", "name": "Aime service", "status": "unknown",
                "detail": "Diagnostics unavailable."}
    try:
        hour = error_store.recent(1)
        day = error_store.recent(24)
    except Exception:
        return {"id": "aime", "name": "Aime service", "status": "unknown",
                "detail": "Diagnostics unavailable."}

    faults = hour.get("unknown", 0)
    if faults >= _OUTAGE_AT:
        status, detail = "outage", "Many unexpected errors in the last hour."
    elif faults >= _DEGRADED_AT:
        noun = "error" if faults == 1 else "errors"
        status = "degraded"
        detail = f"{faults} unexpected {noun} in the last hour."
    elif day.get("unknown", 0):
        status = "operational"
        detail = "Recovered — no errors in the last hour."
    else:
        status = "operational"
        detail = "Operating normally."
    return {"id": "aime", "name": "Aime service",
            "status": status, "detail": detail}


# ---------------------------------------------------------------------------
# Combined snapshot
# ---------------------------------------------------------------------------

def snapshot(error_store) -> dict:
    """The full health snapshot the ``/health`` page and ``/health.json``
    render: an overall roll-up, the per-component breakdown, and a UTC
    timestamp. Components are ordered Aime-first (it's our service) then the
    provider it depends on."""
    components = [aime_component(error_store), anthropic_component()]
    overall = _worst([c["status"] for c in components])
    return {
        "overall": {"status": overall, "label": _OVERALL_LABEL[overall]},
        "components": components,
        "checked_at": datetime.datetime.now(
            datetime.timezone.utc
        ).replace(microsecond=0).isoformat(),
    }
