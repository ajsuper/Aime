"""Web admin dashboard for Aime.

A small Flask app that combines two things behind one password-gated login:

  * **Usage statistics** — the append-only usage log written by `aime.usage`
    (<database>/usage/usage.jsonl), presented as readable tables:
      - Overview        — per-user / per-day / per-model token cost.
      - Cache Efficacy  — whether prompt caching is actually saving money.
  * **Administration** — a web equivalent of the `scripts/` admin CLIs, so a
    container deployment can be managed without shell access:
      - Accounts — list / grant / revoke send access, soft-delete, restore,
                   and purge expired accounts (wraps aime.auth + aime.accounts,
                   the same surface as scripts/access_keys.py + manage_users.py).
      - Keys     — mint and revoke single-use invite keys.

Because the admin tabs can disable accounts, delete data, and spend money, the
whole dashboard sits behind a password gate: set `AIME_ADMIN_PASSWORD` and the
app refuses to start without it. A signed session cookie keeps the admin
logged in; `SameSite=Lax` plus a per-session CSRF token guard the state-
changing POSTs. It still binds loopback by default — `AIME_USAGE_DASHBOARD_HOST`
must be set explicitly (e.g. 0.0.0.0 inside a container, behind a host-only
port mapping) to listen more widely.

The usage tabs refresh on a selectable interval (1s / 30s / 5m, or off); the
refresh re-fetches only the data region and swaps it in place. The admin tabs
do not auto-refresh — they carry forms.

Run from the project's `src/` directory:

    AIME_ADMIN_PASSWORD=... python -m frontends.usage_dashboard

then open http://127.0.0.1:5050/.
"""

import os
import sys
import secrets
import datetime
import zoneinfo
from functools import wraps
from urllib.parse import urlencode

from flask import (
    Flask, render_template_string, request, session, redirect, url_for,
)

# Allow `python -m frontends.usage_dashboard` from src/ to find the aime
# package and the scripts/ directory.
_SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO = os.path.dirname(_SRC)
sys.path.insert(0, _SRC)
sys.path.insert(0, os.path.join(_REPO, "scripts"))

# Reuse the exact cost model, aggregation, and log-path resolution from the
# CLI report so the web view and `usage_report.py` can never disagree on a
# dollar figure.
import usage_report as _report  # noqa: E402

# Account / key administration — the dashboard is a thin wrapper over exactly
# these, the same as the scripts/ CLIs.
from aime import config as _config  # noqa: E402
from aime import auth as _auth  # noqa: E402
from aime import accounts as _accounts  # noqa: E402
from aime import quota as _quota  # noqa: E402
from aime import feedback as _feedback  # noqa: E402
from aime import errors as _errors  # noqa: E402

app = Flask(__name__)

# Session signing key — persisted on disk so a dashboard restart does not log
# the admin out. Mode 0600, alongside the rest of the app data.
app.secret_key = _auth.load_or_create_secret_key(
    os.path.join(_config.DATABASE_DIR, "admin_dashboard_secret.key")
)
# The dashboard is plain HTTP (loopback / host-only port), so SECURE stays
# off; HTTPONLY + SameSite=Lax still block cross-site cookie use, which —
# together with the per-session CSRF token — defends the state-changing POSTs.
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)

# Loopback only by default — see module docstring. AIME_USAGE_DASHBOARD_HOST
# overrides the bind host (e.g. 0.0.0.0 inside a container, where the port is
# only reachable via an explicit Docker mapping). Leave it unset otherwise.
_HOST = os.environ.get("AIME_USAGE_DASHBOARD_HOST", "127.0.0.1")
_PORT = int(os.environ.get("AIME_USAGE_DASHBOARD_PORT", "5050"))

# The gate. Empty means the dashboard refuses to start (see main()).
_ADMIN_PASSWORD = os.environ.get("AIME_ADMIN_PASSWORD", "")

# Per-IP brute-force throttle on the login form: 10 attempts / 5 minutes.
_login_limiter = _auth.IPRateLimiter(limit=10, window_seconds=300)

# Grace period for soft-deleted accounts, mirrored from the CLI default so the
# web and CLI tooling agree on when an account becomes purge-eligible.
_GRACE_DAYS = _accounts.DEFAULT_GRACE_DAYS

# Allowed auto-refresh intervals, in seconds. 0 = off. Anything else is
# rejected back to the default so a hand-edited query string can't wedge the
# page into a 1ms reload loop.
_REFRESH_CHOICES = (0, 1, 30, 300)
# Off by default — the live refresh swaps the data region, which can cancel an
# in-flight click on the chip filter or the Total/Avg toggle. Admins who want
# live polling can re-enable it from the Auto-refresh dropdown.
_REFRESH_DEFAULT = 0

# 5-minute cache TTL, in seconds. Median request spacing above this means a
# 5m-TTL cache write tends to expire before it is ever read back.
_CACHE_5M_TTL = 300

# Time zones offered in the dashboard's tz dropdown. UTC stays first as the
# safe default for shared / production deploys; the rest are picked for the
# current beta cohort. "auto" is resolved client-side via Intl before submit.
_COMMON_TIMEZONES = (
    "UTC",
    "America/Los_Angeles",
    "America/Denver",
    "America/Chicago",
    "America/New_York",
    "Europe/London",
    "Europe/Berlin",
    "Asia/Tokyo",
    "Australia/Sydney",
)


def _resolve_zone(tz_raw: str) -> tuple[zoneinfo.ZoneInfo | None, str]:
    """Map the raw `tz` query arg to (ZoneInfo|None, display label).

    None means UTC — we skip the per-record shift in that case. Unknown names
    (including the literal "auto", which JS is supposed to replace before the
    form submits) also degrade to UTC rather than 500ing the page.
    """
    name = (tz_raw or "").strip()
    if not name or name.upper() == "UTC" or name.lower() == "auto":
        return None, "UTC"
    try:
        return zoneinfo.ZoneInfo(name), name
    except zoneinfo.ZoneInfoNotFoundError:
        return None, "UTC"


def _shift_records_to_zone(records, zone: zoneinfo.ZoneInfo) -> None:
    """Rewrite each record's `ts` field from naive-UTC to naive-local in
    `zone`, in place. Handled per-record so DST transitions inside the visible
    window are converted correctly rather than at a single window-wide offset."""
    for rec in records:
        ts = rec.get("ts")
        if not ts:
            continue
        try:
            dt_utc = datetime.datetime.fromisoformat(ts).replace(
                tzinfo=datetime.timezone.utc)
        except ValueError:
            continue
        rec["ts"] = dt_utc.astimezone(zone).replace(tzinfo=None).isoformat(
            timespec="seconds")


def _local_now(zone: zoneinfo.ZoneInfo | None) -> datetime.datetime:
    """`datetime.now()` in the dashboard's selected display zone, returned as
    a naive value so it can be compared against the (already-shifted) `ts`
    strings without tripping aware/naive type errors."""
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    if zone is None:
        return now_utc.replace(tzinfo=None)
    return now_utc.astimezone(zone).replace(tzinfo=None)

# Lazily-built shared auth backend (holds a sqlite connection). Built on first
# admin use so a usage-only glance never opens auth.sql.
_auth_backend_singleton: _auth.LocalAuthBackend | None = None


def _auth_backend() -> _auth.LocalAuthBackend:
    global _auth_backend_singleton
    if _auth_backend_singleton is None:
        _auth_backend_singleton = _auth.LocalAuthBackend(
            os.path.join(_config.DATABASE_DIR, "auth.sql")
        )
    return _auth_backend_singleton


# Lazily-built usage-budget store (aime.quota), opened only when the Accounts
# tab renders the per-user usage column or a tier is changed.
_quota_store_singleton: _quota.QuotaStore | None = None


def _quota_store() -> _quota.QuotaStore:
    global _quota_store_singleton
    if _quota_store_singleton is None:
        _quota_store_singleton = _quota.QuotaStore(
            os.path.join(_config.DATABASE_DIR, "quota.sql")
        )
    return _quota_store_singleton


# Lazily-built feedback-ticket store (aime.feedback), opened only when the
# Feedback tab renders or a ticket is triaged.
_feedback_store_singleton: _feedback.FeedbackStore | None = None


def _feedback_store() -> _feedback.FeedbackStore:
    global _feedback_store_singleton
    if _feedback_store_singleton is None:
        _feedback_store_singleton = _feedback.FeedbackStore(
            os.path.join(_config.DATABASE_DIR, "feedback.sql")
        )
    return _feedback_store_singleton


# Lazily-built error/diagnostics store (aime.errors), opened only when the
# Errors tab renders or an error row is triaged. Written by the per-user web app.
_error_store_singleton: _errors.ErrorStore | None = None


def _error_store() -> _errors.ErrorStore:
    global _error_store_singleton
    if _error_store_singleton is None:
        _error_store_singleton = _errors.ErrorStore(
            os.path.join(_config.DATABASE_DIR, "errors.sql")
        )
    return _error_store_singleton


def _log_path() -> str:
    """Path to usage.jsonl, resolved identically to the CLI report (honours
    AIME_DATABASE_DIR)."""
    return _report._default_log_path()


def _parse_bound(text: str, *, end: bool):
    """Tolerant version of usage_report._parse_bound for web use.

    Returns (datetime|None, error|None). Unlike the CLI helper this never
    exits the process — a bad date from a query string just yields an error
    string shown back to the user.
    """
    text = (text or "").strip()
    if not text:
        return None, None
    try:
        if len(text) == 10:  # YYYY-MM-DD
            d = datetime.date.fromisoformat(text)
            t = datetime.time(23, 59, 59) if end else datetime.time.min
            return datetime.datetime.combine(d, t), None
        return datetime.datetime.fromisoformat(text), None
    except ValueError:
        return None, f"could not parse date/time: {text!r}"


def _median(values):
    """Median of a non-empty list, else None."""
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def _aggregate_by_day(records):
    """Fold api records into per-day totals (UTC date taken from `ts`)."""
    days = {}
    for rec in records:
        if rec.get("kind") != "api":
            continue
        day = str(rec.get("ts", ""))[:10]
        if not day:
            continue
        d = days.setdefault(day, {"api_calls": 0, "input": 0, "output": 0, "cost": 0.0})
        d["api_calls"] += 1
        d["input"] += rec.get("input_tokens", 0)
        d["output"] += rec.get("output_tokens", 0)
        d["cost"] += _report._api_cost(rec)
    return days


def _aggregate_by_model(records):
    """Fold api records into per-model totals."""
    models = {}
    for rec in records:
        if rec.get("kind") != "api":
            continue
        name = rec.get("model") or "(unknown)"
        m = models.setdefault(name, {"api_calls": 0, "input": 0, "output": 0, "cost": 0.0})
        m["api_calls"] += 1
        m["input"] += rec.get("input_tokens", 0)
        m["output"] += rec.get("output_tokens", 0)
        m["cost"] += _report._api_cost(rec)
    return models


# --- tool-cost model -------------------------------------------------------
#
# The agent doesn't get a per-tool token bill — a turn that emits N tool_use
# blocks is one API call charging for the whole assistant message, and the
# tool_results come back in the *next* user turn's input. So tool cost is an
# attribution. We use the actionable one: each tool record carries the byte
# size of the result it pushed back into the prompt; that becomes fresh input
# tokens on the next turn, billed at the emitting turn's model rate. Removing
# the tool would remove that cost — which is exactly the "what should I cut
# first?" question this view answers. Server tools (web_search) additionally
# pay a flat per-request charge that we count exactly.
#
# 4 bytes per token is the rough UTF-8 average for JSON tool results; close
# enough for a ranking, and the dashboard tooltip flags it as an estimate.
_BYTES_PER_TOKEN = 4.0


def _tool_record_cost(rec) -> float:
    """Estimated USD cost of one tool record.

    Downstream-input estimate (`result_bytes / 4` ≈ tokens, priced at the
    turn's model input rate) plus the exact flat per-request server-tool
    charge for web_search. Output-side cost of the tool_use block itself is
    *not* attributed here — it stays on the API record and shows up under
    the model that produced it; double-counting would over-state savings.
    """
    p = _report._price_for(rec.get("model", ""))
    bytes_ = rec.get("result_bytes", 0) or 0
    tokens = bytes_ / _BYTES_PER_TOKEN
    token_cost = tokens * p["in"] / 1_000_000.0
    flat = (rec.get("web_search_requests", 0) or 0) * _report.WEB_SEARCH_COST_PER_REQUEST
    return token_cost + flat


def _aggregate_by_tool(records):
    """Fold tool records into per-tool totals.

    Returns `{tool_name: {calls, kind, result_bytes, web_search_requests,
    cost}}` keyed by the agent-side tool name (e.g. ``CreateEvent``,
    ``web_search``). `kind` is the most recently seen "client" / "server"
    label — every record for a given tool name uses the same one in practice,
    so this is just a display hint for the table.
    """
    tools = {}
    for rec in records:
        if rec.get("kind") != "tool":
            continue
        name = rec.get("tool_name") or "(unknown)"
        t = tools.setdefault(name, {
            "calls": 0, "kind": rec.get("tool_kind") or "client",
            "result_bytes": 0, "web_search_requests": 0, "cost": 0.0,
        })
        t["calls"] += 1
        t["kind"] = rec.get("tool_kind") or t["kind"]
        t["result_bytes"] += rec.get("result_bytes", 0) or 0
        t["web_search_requests"] += rec.get("web_search_requests", 0) or 0
        t["cost"] += _tool_record_cost(rec)
    return tools


def _aggregate_tool_per_day(records, day_keys):
    """Per-tool daily cost series, aligned to `day_keys`. Same shape as
    `_aggregate_by_day_model` — the Tools tab stacks these into a daily bar
    chart and feeds them to the per-tool sparklines."""
    idx = {d: i for i, d in enumerate(day_keys)}
    out = {}
    for rec in records:
        if rec.get("kind") != "tool":
            continue
        day = str(rec.get("ts", ""))[:10]
        if day not in idx:
            continue
        name = rec.get("tool_name") or "(unknown)"
        row = out.setdefault(name, [0.0] * len(day_keys))
        row[idx[day]] += _tool_record_cost(rec)
    # Sort tools by total cost desc — drives stacking order and palette indexing.
    return sorted(out.items(), key=lambda kv: sum(kv[1]), reverse=True)


def _aggregate_agents(records):
    """Fold the background-agent records into per-*user* totals.

    Only records stamped ``source == "agent"`` (set by the background-agent
    runner on its backend, web-search sub-agent and tool calls) count here, so
    interactive chat cost is excluded. Keying by user — rather than by an agent
    name — is deliberate: agent names are unbounded (every user can define
    several), so the question worth answering on a shared deployment is "how
    much do *this user's* agents cost?".

    Cost is the *api-record* cost — the same real, billed basis as the
    Overview/per-user figure — so a user's agent spend is directly comparable
    to their live-chat spend. Tool records contribute their call count (what
    the agents actually did) but not extra cost: their downstream-input cost is
    already billed on the following turn's api record, and adding the Tools-tab
    estimate on top would double-count the real bill.

    ``runs`` counts distinct ``session_id``s — each background run opens its
    own in-memory session, so this is the number of agent runs for the user. It
    requires ``AIME_USAGE_LINK_USERS=1`` (session_id is null otherwise) and
    reads 0 when linkage is off.
    """
    users = {}
    for rec in records:
        if (rec.get("source") or "interactive") != "agent":
            continue
        name = rec.get("user") or "(anonymous)"
        a = users.setdefault(name, {
            "api_calls": 0, "tool_calls": 0, "input": 0, "output": 0,
            "cache_r": 0, "cache_w": 0, "web_searches": 0, "cost": 0.0,
            "purposes": {}, "_sessions": set(),
        })
        sid = rec.get("session_id")
        if sid:
            a["_sessions"].add(sid)
        kind = rec.get("kind")
        if kind == "api":
            cc_5m, cc_1h = _report._cache_write_tokens(rec)
            cost = _report._api_cost(rec)
            a["api_calls"]    += 1
            a["input"]        += rec.get("input_tokens", 0)
            a["output"]       += rec.get("output_tokens", 0)
            a["cache_r"]      += rec.get("cache_read_tokens", 0)
            a["cache_w"]      += cc_5m + cc_1h
            a["web_searches"] += rec.get("web_search_requests", 0)
            a["cost"]         += cost
            purpose = rec.get("purpose") or "(unspecified)"
            a["purposes"][purpose] = a["purposes"].get(purpose, 0.0) + cost
        elif kind == "tool":
            a["tool_calls"] += 1
    for a in users.values():
        a["runs"] = len(a.pop("_sessions"))
        a["cost_per_run"] = (a["cost"] / a["runs"]) if a["runs"] else 0.0
        # Compact "turn 62% · web_search 30% · …" mix string for the table.
        total = a["cost"] or 0.0
        a["purpose_mix"] = ", ".join(
            f"{p} {100.0 * c / total:.0f}%"
            for p, c in sorted(a["purposes"].items(), key=lambda kv: kv[1], reverse=True)
        ) if total else ""
    return users


def _aggregate_agent_per_day(records, day_keys):
    """Per-user agent api cost by day, aligned to `day_keys` — same shape as
    `_aggregate_by_day_model`; the Agents tab stacks these into a daily bar
    chart and feeds the per-user sparklines."""
    idx = {d: i for i, d in enumerate(day_keys)}
    out = {}
    for rec in records:
        if rec.get("kind") != "api" or (rec.get("source") or "interactive") != "agent":
            continue
        day = str(rec.get("ts", ""))[:10]
        if day not in idx:
            continue
        name = rec.get("user") or "(anonymous)"
        row = out.setdefault(name, [0.0] * len(day_keys))
        row[idx[day]] += _report._api_cost(rec)
    return sorted(out.items(), key=lambda kv: sum(kv[1]), reverse=True)


def _prompt_costs(rec):
    """Return (with_cache, without_cache) prompt-token cost for an api record.

    Only the prompt side is modelled — output and web-search charges are
    identical whether or not caching is on, so they cancel out of any saving.

    With caching, each token is billed at its actual rate (fresh input 1x,
    cache read 0.1x, 5m write 1.25x, 1h write 2x). Without caching, every one
    of those tokens would instead be sent as plain input at the 1x base rate.
    """
    p = _report._price_for(rec.get("model", ""))
    base = p["in"] / 1_000_000.0
    cc_5m, cc_1h = _report._cache_write_tokens(rec)
    cr = rec.get("cache_read_tokens", 0)
    fresh = rec.get("input_tokens", 0)
    with_cache = base * (
        fresh
        + cr * _report.CACHE_READ_MULT
        + cc_5m * _report.CACHE_WRITE_5M_MULT
        + cc_1h * _report.CACHE_WRITE_1H_MULT
    )
    without_cache = base * (fresh + cr + cc_5m + cc_1h)
    return with_cache, without_cache


def _aggregate_cache(records):
    """Per-user cache-efficacy figures, derived from api records."""
    users = {}
    for rec in records:
        if rec.get("kind") != "api":
            continue
        name = rec.get("user") or "(anonymous)"
        u = users.setdefault(name, {
            "calls": 0, "fresh": 0, "reads": 0, "w5m": 0, "w1h": 0,
            "with_cache": 0.0, "without_cache": 0.0, "_ts": [],
        })
        cc_5m, cc_1h = _report._cache_write_tokens(rec)
        wc, nc = _prompt_costs(rec)
        u["calls"] += 1
        u["fresh"] += rec.get("input_tokens", 0)
        u["reads"] += rec.get("cache_read_tokens", 0)
        u["w5m"] += cc_5m
        u["w1h"] += cc_1h
        u["with_cache"] += wc
        u["without_cache"] += nc
        try:
            u["_ts"].append(datetime.datetime.fromisoformat(rec["ts"]))
        except (ValueError, KeyError):
            pass

    for u in users.values():
        writes = u["w5m"] + u["w1h"]
        u["writes"] = writes
        # Reads per token written: how many times the average cached segment
        # is reused. Below ~1 the cache is barely earning its write premium.
        u["reuse"] = (u["reads"] / writes) if writes else 0.0
        u["savings"] = u["without_cache"] - u["with_cache"]
        u["savings_pct"] = (
            100.0 * u["savings"] / u["without_cache"] if u["without_cache"] else 0.0
        )
        # Written tokens that were never read back — a lower bound on wasted
        # cache writes (paid the write premium, got no read discount).
        u["unread_writes"] = max(0, writes - u["reads"])
        ts = sorted(u.pop("_ts"))
        gaps = [(b - a).total_seconds() for a, b in zip(ts, ts[1:])]
        u["median_gap"] = _median(gaps)
        # A 5m write is at risk when the typical gap between this user's
        # requests outlives the 5-minute TTL.
        u["ttl_risk"] = (
            u["median_gap"] is not None
            and u["median_gap"] > _CACHE_5M_TTL
            and u["w5m"] > 0
        )
    return users


def _haiku_price() -> dict:
    """Look up Haiku's base prices via the same prefix-match path the
    dashboard uses for any other model id. Centralised here so a model id
    change in usage_report.PRICES propagates to the routing tab."""
    return _report._price_for("claude-haiku-4-5")


def _sonnet_price() -> dict:
    return _report._price_for("claude-sonnet-4-6")


def _api_cost_at(rec: dict, price: dict) -> float:
    """Same shape as ``_report._api_cost``, but bills the record's token
    counts at an arbitrary ``price`` dict instead of the model the record
    actually used. Lets the routing tab compute the counterfactual cost of
    every Haiku-routed turn as if Sonnet had handled it (and vice versa).
    Web-search is a flat per-request charge and is identical regardless of
    routing, so it carries straight through."""
    cc_5m, cc_1h = _report._cache_write_tokens(rec)
    token_cost = (
        rec.get("input_tokens", 0)        * price["in"]
        + rec.get("output_tokens", 0)     * price["out"]
        + rec.get("cache_read_tokens", 0) * price["in"] * _report.CACHE_READ_MULT
        + cc_5m                           * price["in"] * _report.CACHE_WRITE_5M_MULT
        + cc_1h                           * price["in"] * _report.CACHE_WRITE_1H_MULT
    ) / 1_000_000.0
    return token_cost + rec.get("web_search_requests", 0) * _report.WEB_SEARCH_COST_PER_REQUEST


def _aggregate_routing(records):
    """Build the Model Routing tab's figures.

    Looks at api records carrying ``routed_decision`` (set by the backend
    when the router picked the model for that turn) and at api records
    with ``purpose == "route"`` (the classifier calls themselves).

    For each routed turn:
      * ``actual``        — what the call billed (same as ``_api_cost``).
      * ``counterfactual`` — what the SAME token counts would have billed
                             if priced at the OTHER pole's rate. For
                             Haiku-routed turns this is the Sonnet cost we
                             avoided; for Sonnet-routed turns it is the
                             (hypothetical) Haiku cost — surfaced as info
                             only, since we don't claim savings on those.

    Per-user and overall figures:
      * ``haiku_turns`` / ``sonnet_turns``
      * ``haiku_savings``  — sum of (counterfactual − actual) on
                             Haiku-routed turns. Always >= 0 by construction
                             of the price table.
      * ``router_cost``    — sum of actual cost of ``purpose="route"``
                             classifier calls.
      * ``net_savings``    — ``haiku_savings - router_cost``.
      * ``maybe_misclass`` — Haiku-routed turns whose stop_reason is
                             "max_tokens" OR whose token-count looks
                             Sonnet-sized (>3000 input tokens). Hints that
                             the classifier is being too generous.
    """
    sonnet_p = _sonnet_price()
    haiku_p = _haiku_price()
    users: dict = {}
    for rec in records:
        if rec.get("kind") != "api":
            continue
        name = rec.get("user") or "(anonymous)"
        u = users.setdefault(name, {
            "haiku_turns": 0, "sonnet_turns": 0,
            "haiku_actual": 0.0, "haiku_counterfactual": 0.0,
            "sonnet_actual": 0.0, "sonnet_counterfactual": 0.0,
            "router_calls": 0, "router_cost": 0.0,
            "maybe_misclass": 0,
        })
        purpose = rec.get("purpose") or ""
        decision = rec.get("routed_decision")
        if purpose == "route":
            u["router_calls"] += 1
            u["router_cost"] += _report._api_cost(rec)
            continue
        if decision == "haiku":
            actual = _api_cost_at(rec, haiku_p)
            counter = _api_cost_at(rec, sonnet_p)
            u["haiku_turns"] += 1
            u["haiku_actual"] += actual
            u["haiku_counterfactual"] += counter
            if (
                rec.get("stop_reason") == "max_tokens"
                or rec.get("input_tokens", 0) > 3000
            ):
                u["maybe_misclass"] += 1
        elif decision == "sonnet":
            actual = _api_cost_at(rec, sonnet_p)
            counter = _api_cost_at(rec, haiku_p)
            u["sonnet_turns"] += 1
            u["sonnet_actual"] += actual
            u["sonnet_counterfactual"] += counter
    for u in users.values():
        u["haiku_savings"] = max(
            0.0, u["haiku_counterfactual"] - u["haiku_actual"]
        )
        u["net_savings"] = u["haiku_savings"] - u["router_cost"]
        u["total_turns"] = u["haiku_turns"] + u["sonnet_turns"]
        u["haiku_pct"] = (
            100.0 * u["haiku_turns"] / u["total_turns"]
            if u["total_turns"] else 0.0
        )
    return users


def _routing_daily(records, day_keys):
    """Two parallel series of length ``len(day_keys)``: Haiku-routed turns
    per day and Sonnet-routed turns per day. Drives the stacked bar chart
    on the routing tab."""
    idx = {d: i for i, d in enumerate(day_keys)}
    haiku = [0] * len(day_keys)
    sonnet = [0] * len(day_keys)
    for rec in records:
        if rec.get("kind") != "api":
            continue
        day = (rec.get("ts") or "")[:10]
        if day not in idx:
            continue
        decision = rec.get("routed_decision")
        if decision == "haiku":
            haiku[idx[day]] += 1
        elif decision == "sonnet":
            sonnet[idx[day]] += 1
    return haiku, sonnet


def _aggregate_web_search(records):
    """Web-search offload savings.

    Web search runs on a Haiku sub-agent (``purpose == "web_search"``): it does
    the searching, reads the raw results, and hands the conversational model a
    compact digest. Re-pricing the sub-agent's token counts at Sonnet's rate
    approximates what the expensive model would have spent ingesting those same
    raw results inline — a conservative floor on the saving, since it ignores
    the bigger recurring win (those results never enter the conversation, so
    they're never re-read from cache on later turns or re-summarised at
    compaction).

    The flat per-search fee ($10 / 1000) is charged whoever runs the search, so
    it lands in both ``actual`` and ``counterfactual`` (via ``_api_cost_at``)
    and cancels out of the saving; it's surfaced separately as ``flat_cost``.
    """
    sonnet_p = _sonnet_price()
    haiku_p = _haiku_price()
    out = {
        "calls": 0, "searches": 0,
        "actual": 0.0, "counterfactual": 0.0, "savings": 0.0, "flat_cost": 0.0,
    }
    for rec in records:
        if rec.get("kind") != "api" or (rec.get("purpose") or "") != "web_search":
            continue
        out["calls"] += 1
        out["actual"] += _api_cost_at(rec, haiku_p)
        out["counterfactual"] += _api_cost_at(rec, sonnet_p)
        n = rec.get("web_search_requests", 0) or 0
        out["searches"] += n
        out["flat_cost"] += n * _report.WEB_SEARCH_COST_PER_REQUEST
    out["savings"] = max(0.0, out["counterfactual"] - out["actual"])
    return out


def _percentile(sorted_values, pct: float):
    """Linear-interpolated percentile of a pre-sorted list (or None if empty)."""
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return sorted_values[0]
    k = (pct / 100.0) * (len(sorted_values) - 1)
    lo = int(k)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = k - lo
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * frac


def _aggregate_purpose(records):
    """Per-purpose totals (turn / title / compaction / ...).

    Purpose tags what an API call was *for* — user-facing turns vs. cheap
    background Haiku tasks like session-title generation and history
    compaction. Splitting by purpose lets the admin see how much of the bill
    is the user actually talking, vs. plumbing they never see.
    """
    purposes = {}
    for rec in records:
        if rec.get("kind") != "api":
            continue
        name = rec.get("purpose") or "(unspecified)"
        p = purposes.setdefault(name, {
            "calls": 0, "input": 0, "output": 0, "cost": 0.0, "_lat": [],
        })
        p["calls"] += 1
        p["input"] += rec.get("input_tokens", 0)
        p["output"] += rec.get("output_tokens", 0)
        p["cost"] += _report._api_cost(rec)
        d = rec.get("duration_ms")
        if d is not None:
            try:
                p["_lat"].append(float(d))
            except (TypeError, ValueError):
                pass
    for p in purposes.values():
        lats = sorted(p.pop("_lat"))
        p["lat_n"] = len(lats)
        p["lat_p50"] = _percentile(lats, 50)
        p["lat_p90"] = _percentile(lats, 90)
        p["lat_p99"] = _percentile(lats, 99)
    return purposes


def _aggregate_stop_reasons(records):
    """Counts per stop_reason, plus the total of records that carried one.

    Returns (counts_dict, total_with_reason). end_turn = clean finish,
    tool_use = handed off to a tool, max_tokens = ran into the output cap.
    A growing max_tokens share is the usual signal to raise the limit.
    """
    counts = {}
    total = 0
    for rec in records:
        if rec.get("kind") != "api":
            continue
        r = rec.get("stop_reason")
        if not r:
            continue
        counts[r] = counts.get(r, 0) + 1
        total += 1
    return counts, total


def _aggregate_hour(records):
    """API calls bucketed by UTC hour-of-day (0..23)."""
    hours = [0] * 24
    for rec in records:
        if rec.get("kind") != "api":
            continue
        try:
            hours[datetime.datetime.fromisoformat(rec["ts"]).hour] += 1
        except (ValueError, KeyError):
            continue
    return hours


def _overall_latency(records):
    """Sorted list of every api record's duration_ms (those that carry one)."""
    lats = []
    for rec in records:
        if rec.get("kind") != "api":
            continue
        d = rec.get("duration_ms")
        if d is None:
            continue
        try:
            lats.append(float(d))
        except (TypeError, ValueError):
            pass
    lats.sort()
    return lats


def _format_bytes(n: int) -> str:
    """Human-friendly byte size — KB / MB / GB with one decimal."""
    if n < 1024:
        return f"{n} B"
    for unit in ("KB", "MB", "GB", "TB"):
        n /= 1024.0
        if n < 1024:
            return f"{n:.1f} {unit}"
    return f"{n:.1f} PB"


def _dir_size(path: str) -> int:
    """Recursive byte size of a directory tree. Missing path → 0. Best-effort:
    a file that vanishes between stat calls is skipped, never raised."""
    if not os.path.isdir(path):
        return 0
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def _count_files(path: str, suffix: str | None = None) -> int:
    """Number of files (optionally only those ending in `suffix`) in a
    directory. Does NOT read file contents — directory listing only."""
    if not os.path.isdir(path):
        return 0
    try:
        entries = os.listdir(path)
    except OSError:
        return 0
    if suffix is None:
        return sum(1 for e in entries if os.path.isfile(os.path.join(path, e)))
    return sum(
        1 for e in entries
        if e.endswith(suffix) and os.path.isfile(os.path.join(path, e))
    )


# ---------------------------------------------------------------------------
# Extended aggregations (chart-shaped)
# ---------------------------------------------------------------------------


def _day_keys_in_range(records):
    """Sorted list of UTC dates covered by `records`, gap-free.

    Returns ``[]`` for empty input. The result is dense — every calendar date
    between the earliest and latest record appears, even if no traffic
    happened on it — so a line/area chart draws an honest zero, not a
    visually-misleading skip.
    """
    days = set()
    for rec in records:
        d = str(rec.get("ts", ""))[:10]
        if d:
            days.add(d)
    if not days:
        return []
    lo = datetime.date.fromisoformat(min(days))
    hi = datetime.date.fromisoformat(max(days))
    out = []
    cur = lo
    while cur <= hi:
        out.append(cur.isoformat())
        cur += datetime.timedelta(days=1)
    return out


def _aggregate_by_day_model(records, day_keys):
    """Per-day, per-model USD cost — shape: { model: [cost_per_day, ...] }.

    Daily costs are returned in the same order as ``day_keys`` (which is the
    dense day axis from ``_day_keys_in_range``). Models are returned ordered
    by total cost descending — handy for the stacked bar's z-order.
    """
    idx = {d: i for i, d in enumerate(day_keys)}
    series = {}
    for rec in records:
        if rec.get("kind") != "api":
            continue
        day = str(rec.get("ts", ""))[:10]
        if day not in idx:
            continue
        model = rec.get("model") or "(unknown)"
        row = series.setdefault(model, [0.0] * len(day_keys))
        row[idx[day]] += _report._api_cost(rec)
    # Order by total cost so the largest contributor draws at the base.
    return sorted(series.items(), key=lambda kv: sum(kv[1]), reverse=True)


def _aggregate_active_users_per_day(records, day_keys):
    """Distinct users per UTC day, aligned to ``day_keys``."""
    idx = {d: i for i, d in enumerate(day_keys)}
    buckets = [set() for _ in day_keys]
    for rec in records:
        if rec.get("kind") != "api":
            continue
        day = str(rec.get("ts", ""))[:10]
        if day not in idx:
            continue
        buckets[idx[day]].add(rec.get("user") or "(anonymous)")
    return [len(b) for b in buckets]


def _aggregate_calls_per_day(records, day_keys):
    """API call count per day, aligned to ``day_keys``."""
    idx = {d: i for i, d in enumerate(day_keys)}
    out = [0] * len(day_keys)
    for rec in records:
        if rec.get("kind") != "api":
            continue
        day = str(rec.get("ts", ""))[:10]
        if day in idx:
            out[idx[day]] += 1
    return out


def _aggregate_cost_per_day(records, day_keys):
    """Daily total USD cost (all charges), aligned to ``day_keys``."""
    idx = {d: i for i, d in enumerate(day_keys)}
    out = [0.0] * len(day_keys)
    for rec in records:
        if rec.get("kind") != "api":
            continue
        day = str(rec.get("ts", ""))[:10]
        if day in idx:
            out[idx[day]] += _report._api_cost(rec)
    return out


def _aggregate_cache_savings_per_day(records, day_keys):
    """Per-day prompt-side cache savings (no-cache cost − actual cost)."""
    idx = {d: i for i, d in enumerate(day_keys)}
    out = [0.0] * len(day_keys)
    for rec in records:
        if rec.get("kind") != "api":
            continue
        day = str(rec.get("ts", ""))[:10]
        if day not in idx:
            continue
        wc, nc = _prompt_costs(rec)
        out[idx[day]] += (nc - wc)
    return out


def _aggregate_purpose_per_day(records, day_keys):
    """Per-day call count per purpose. Same shape as _aggregate_by_day_model."""
    idx = {d: i for i, d in enumerate(day_keys)}
    series = {}
    for rec in records:
        if rec.get("kind") != "api":
            continue
        day = str(rec.get("ts", ""))[:10]
        if day not in idx:
            continue
        purpose = rec.get("purpose") or "(unspecified)"
        row = series.setdefault(purpose, [0] * len(day_keys))
        row[idx[day]] += 1
    return sorted(series.items(), key=lambda kv: sum(kv[1]), reverse=True)


def _latency_per_day(records, day_keys):
    """Per-day p50/p90 latency in ms (None where no records carry duration_ms)."""
    idx = {d: i for i, d in enumerate(day_keys)}
    buckets = [[] for _ in day_keys]
    for rec in records:
        if rec.get("kind") != "api":
            continue
        day = str(rec.get("ts", ""))[:10]
        if day not in idx:
            continue
        d = rec.get("duration_ms")
        if d is None:
            continue
        try:
            buckets[idx[day]].append(float(d))
        except (TypeError, ValueError):
            pass
    p50 = []
    p90 = []
    for b in buckets:
        if not b:
            p50.append(None)
            p90.append(None)
        else:
            b.sort()
            p50.append(_percentile(b, 50))
            p90.append(_percentile(b, 90))
    return p50, p90


def _top_calls(records, n=15):
    """The N most expensive single API calls in the window — for the Trends
    'biggest turns' table. Each row is a (cost, record) pair."""
    scored = []
    for rec in records:
        if rec.get("kind") != "api":
            continue
        scored.append((_report._api_cost(rec), rec))
    scored.sort(key=lambda kv: kv[0], reverse=True)
    return scored[:n]


def _per_user_sparkline_data(records, day_keys):
    """Per-user daily cost — { user: [cost_per_day, ...] } aligned to day_keys.

    Used to draw a sparkline alongside the Overview by-user table so the admin
    can spot which users are trending up without leaving the page.
    """
    idx = {d: i for i, d in enumerate(day_keys)}
    out = {}
    for rec in records:
        if rec.get("kind") != "api":
            continue
        day = str(rec.get("ts", ""))[:10]
        if day not in idx:
            continue
        user = rec.get("user") or "(anonymous)"
        row = out.setdefault(user, [0.0] * len(day_keys))
        row[idx[day]] += _report._api_cost(rec)
    return out


def _per_model_sparkline_data(records, day_keys):
    """Per-model daily cost (same shape as the per-user one)."""
    idx = {d: i for i, d in enumerate(day_keys)}
    out = {}
    for rec in records:
        if rec.get("kind") != "api":
            continue
        day = str(rec.get("ts", ""))[:10]
        if day not in idx:
            continue
        model = rec.get("model") or "(unknown)"
        row = out.setdefault(model, [0.0] * len(day_keys))
        row[idx[day]] += _report._api_cost(rec)
    return out


def _compare_periods(all_records, since, until, *, now=None):
    """Headline figures for the current window and the immediately-preceding
    equal-length window — for the Trends tab's period-over-period KPI cards.

    Returns ``(current, previous)`` where each is a dict of ``cost / calls /
    tokens / users``. When ``since`` / ``until`` is unset the comparison falls
    back to the last 30 days vs the 30 days before that, so the panel never
    needs to render as "no comparison available" simply because the admin did
    not type a date.
    """
    # Naive value in the dashboard's selected display zone, to match the (also
    # shifted) `ts` strings on each record.
    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    end = until or now
    if since:
        start = since
    else:
        start = end - datetime.timedelta(days=30)
    span = end - start
    prev_end = start
    prev_start = start - span

    def _sum(records, lo, hi):
        cost = 0.0
        calls = 0
        tokens = 0
        users = set()
        for rec in records:
            if rec.get("kind") != "api":
                continue
            try:
                ts = datetime.datetime.fromisoformat(rec["ts"])
            except (ValueError, KeyError):
                continue
            if ts < lo or ts > hi:
                continue
            cost += _report._api_cost(rec)
            calls += 1
            tokens += rec.get("input_tokens", 0) + rec.get("output_tokens", 0)
            users.add(rec.get("user") or "(anonymous)")
        return {"cost": cost, "calls": calls, "tokens": tokens, "users": len(users)}

    return (
        _sum(all_records, start, end),
        _sum(all_records, prev_start, prev_end),
        (start, end, prev_start, prev_end),
    )


def _delta_pct(curr, prev):
    """Percent change from prev → curr, clamped for display. None when prev=0."""
    if not prev:
        return None
    return 100.0 * (curr - prev) / prev


def _anomaly_days(daily_cost, day_keys):
    """Days whose cost exceeds mean + 2*stddev — naive outlier flag.

    Returns ``[(day, cost, z), ...]`` ordered by date. A real anomaly system
    would seasonal-decompose first; for an admin glance the 2σ rule is fine
    and stays interpretable. Requires at least 4 days of data; otherwise the
    standard deviation is too noisy to be useful and the function returns ``[]``.
    """
    if len(daily_cost) < 4:
        return []
    n = len(daily_cost)
    mean = sum(daily_cost) / n
    var = sum((x - mean) ** 2 for x in daily_cost) / n
    sd = var ** 0.5
    if sd == 0:
        return []
    out = []
    for day, cost in zip(day_keys, daily_cost):
        z = (cost - mean) / sd
        if z >= 2.0:
            out.append((day, cost, z))
    return out


def _new_users_per_day(backend, day_keys):
    """Per-day count of newly-created accounts, aligned to ``day_keys``.

    Best-effort: if the backend doesn't expose creation timestamps the function
    returns an all-zero series, so the chart degrades to a flat baseline
    rather than a crash.
    """
    idx = {d: i for i, d in enumerate(day_keys)}
    out = [0] * len(day_keys)
    try:
        users = backend.list_users() + backend.list_deleted_users()
    except Exception:
        return out
    for u in users:
        created = getattr(u, "created_at", None) or getattr(u, "created", None)
        if not created:
            continue
        day = str(created)[:10]
        if day in idx:
            out[idx[day]] += 1
    return out


def _daily_summary(records, day_keys):
    """Rich per-day row: calls, active users, cost, $/call, p50 latency,
    cache hit %, top model (by cost), top user (by cost).

    One row per ``day_keys`` entry, in the same dense order so the table on
    the page never has visual gaps for quiet days.
    """
    idx = {d: i for i, d in enumerate(day_keys)}
    rows = [
        {
            "day": d, "calls": 0, "users": set(),
            "input": 0, "cache_r": 0, "cost": 0.0,
            "lats": [],
            "by_model": {}, "by_user": {},
        }
        for d in day_keys
    ]
    for rec in records:
        if rec.get("kind") != "api":
            continue
        day = str(rec.get("ts", ""))[:10]
        if day not in idx:
            continue
        r = rows[idx[day]]
        c = _report._api_cost(rec)
        r["calls"] += 1
        r["users"].add(rec.get("user") or "(anonymous)")
        r["input"] += rec.get("input_tokens", 0)
        r["cache_r"] += rec.get("cache_read_tokens", 0)
        r["cost"] += c
        d = rec.get("duration_ms")
        if d is not None:
            try:
                r["lats"].append(float(d))
            except (TypeError, ValueError):
                pass
        model = rec.get("model") or "(unknown)"
        user = rec.get("user") or "(anonymous)"
        r["by_model"][model] = r["by_model"].get(model, 0.0) + c
        r["by_user"][user] = r["by_user"].get(user, 0.0) + c

    out = []
    for r in rows:
        lats = sorted(r["lats"])
        denom = r["input"] + r["cache_r"]
        out.append({
            "day": r["day"],
            "calls": r["calls"],
            "active_users": len(r["users"]),
            "cost": r["cost"],
            "cost_per_call": (r["cost"] / r["calls"]) if r["calls"] else 0.0,
            "p50": _percentile(lats, 50) if lats else None,
            "cache_hit_pct": (100.0 * r["cache_r"] / denom) if denom else 0.0,
            "top_model": max(r["by_model"].items(), key=lambda kv: kv[1])[0]
                if r["by_model"] else "—",
            "top_user": max(r["by_user"].items(), key=lambda kv: kv[1])[0]
                if r["by_user"] else "—",
        })
    return out


def _weekday_hour_heatmap(records):
    """7×24 grid of call counts keyed by (UTC weekday, UTC hour-of-day).

    Weekday axis is Mon..Sun, matching how a calendar-week dashboard would be
    read. Empty input returns a zero grid so the heatmap still draws.
    """
    grid = [[0] * 24 for _ in range(7)]
    for rec in records:
        if rec.get("kind") != "api":
            continue
        try:
            ts = datetime.datetime.fromisoformat(rec["ts"])
        except (ValueError, KeyError):
            continue
        grid[ts.weekday()][ts.hour] += 1
    return grid


def _rolling_average(values, window=7):
    """Centred trailing rolling mean for charts — empty / None-tolerant.

    Padding the front with the running mean keeps the line visible from day
    one rather than only kicking in at index ``window-1`` (which would draw a
    confusing gap on a 5-day window).
    """
    out = []
    acc = []
    for v in values:
        acc.append(v)
        if len(acc) > window:
            acc.pop(0)
        out.append(sum(acc) / len(acc))
    return out


def _classify_users(all_records, *, since, until, dormant_days=14, now=None):
    """Per-user activity classification — three statuses, one engagement pattern.

    Status (mutually exclusive):
      * ``new``     — first-ever record falls inside the window.
      * ``active``  — at least one record in the window (and not new).
      * ``dormant`` — no record in the window AND last record is at least
                      ``dormant_days`` old relative to the window end.

    Engagement pattern (derived from window-only activity, by *day presence*):
      * ``daily``      — appears on every UTC day of the window.
      * ``most-days``  — appears on ≥50% of the window's days (and >1 day).
      * ``occasional`` — appears on 2 or more days, below half.
      * ``once``       — appears on exactly one day.
      * ``none``       — no records in the window.

    A separate ``heavy`` flag fires when the user averages ≥5 calls per
    active day — that's the "logged on multiple times per day" cohort the
    admin asked for, surfaced as a filter chip rather than a status.
    """
    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    end = until or now
    start = since or (end - datetime.timedelta(days=30))
    window_days = max(1, (end.date() - start.date()).days + 1)

    per = {}
    for rec in all_records:
        if rec.get("kind") != "api":
            continue
        try:
            ts = datetime.datetime.fromisoformat(rec["ts"])
        except (ValueError, KeyError):
            continue
        name = rec.get("user") or "(anonymous)"
        p = per.setdefault(name, {
            "first_seen": ts, "last_seen": ts,
            "lifetime_calls": 0, "lifetime_cost": 0.0,
            "window_calls": 0, "window_cost": 0.0,
            "window_days": set(),
        })
        if ts < p["first_seen"]:
            p["first_seen"] = ts
        if ts > p["last_seen"]:
            p["last_seen"] = ts
        p["lifetime_calls"] += 1
        cost = _report._api_cost(rec)
        p["lifetime_cost"] += cost
        if start <= ts <= end:
            p["window_calls"] += 1
            p["window_cost"] += cost
            p["window_days"].add(ts.date())

    new_users = []
    dormant = []
    rows = []
    for name, p in per.items():
        days_since = (end - p["last_seen"]).days
        active_days = len(p["window_days"])
        in_window = p["window_calls"] > 0
        is_new = start <= p["first_seen"] <= end

        if is_new:
            status = "new"
            new_users.append(name)
        elif in_window:
            status = "active"
        elif days_since >= dormant_days:
            status = "dormant"
            dormant.append(name)
        else:
            # No window activity, last seen recently — count as dormant too
            # rather than carry a separate 'inactive' status that the admin
            # would just have to mentally fold together with dormant anyway.
            status = "dormant"
            dormant.append(name)

        if active_days == 0:
            pattern = "none"
        elif active_days >= window_days:
            pattern = "daily"
        elif active_days * 2 >= window_days and active_days > 1:
            pattern = "most-days"
        elif active_days >= 2:
            pattern = "occasional"
        else:
            pattern = "once"

        calls_per_active_day = (
            p["window_calls"] / active_days if active_days else 0.0
        )
        heavy = calls_per_active_day >= 5.0

        rows.append({
            "username": name,
            "first_seen": p["first_seen"].isoformat(timespec="seconds"),
            "last_seen": p["last_seen"].isoformat(timespec="seconds"),
            "days_since_last": days_since,
            "lifetime_calls": p["lifetime_calls"],
            "lifetime_cost": p["lifetime_cost"],
            "window_calls": p["window_calls"],
            "window_cost": p["window_cost"],
            "active_days": active_days,
            "calls_per_active_day": calls_per_active_day,
            "pattern": pattern,
            "heavy": heavy,
            "status": status,
        })
    rows.sort(key=lambda r: r["lifetime_cost"], reverse=True)
    return {
        "rows": rows,
        "new": sorted(new_users),
        "dormant": sorted(dormant, key=lambda n:
                          next(r["days_since_last"] for r in rows
                               if r["username"] == n), reverse=True),
        "window": (start, end),
        "window_days": window_days,
    }


# ---------------------------------------------------------------------------
# Session-grouped aggregations
#
# Records carry `session_id` when AIME_USAGE_LINK_USERS=1 (and the backend
# stamps it). Grouping by session_id turns the log into "one row per
# conversation" — the most direct way to answer "what kinds of conversations
# are actually happening, and which ones are expensive?".
# ---------------------------------------------------------------------------


def _aggregate_by_session(records):
    """Group api + tool records by session_id; return rows ready to render.

    Each row carries: user, started, last_active, msgs (assistant turns),
    api_calls, tool_calls, total_cost, top_model, top_tool, has_compaction,
    has_max_tokens. Records without a session_id (anonymized mode, or
    pre-session-id logs) are skipped — they cannot be grouped meaningfully.
    """
    sessions: dict = {}
    for rec in records:
        sid = rec.get("session_id")
        if not sid:
            continue
        s = sessions.setdefault(sid, {
            "session_id": sid,
            "user": rec.get("user") or "(anonymous)",
            "first_ts": rec.get("ts") or "",
            "last_ts": rec.get("ts") or "",
            "api_calls": 0,
            "tool_calls": 0,
            "turns": 0,
            "compaction_calls": 0,
            "max_tokens_hits": 0,
            "cost": 0.0,
            "by_model": {},
            "by_tool": {},
        })
        ts = rec.get("ts") or ""
        if ts and ts < s["first_ts"]:
            s["first_ts"] = ts
        if ts and ts > s["last_ts"]:
            s["last_ts"] = ts
        # Take a user from any record that has one — earlier (anonymized)
        # records may leave it null.
        if rec.get("user") and s["user"] == "(anonymous)":
            s["user"] = rec["user"]
        kind = rec.get("kind")
        if kind == "api":
            s["api_calls"] += 1
            cost = _report._api_cost(rec)
            s["cost"] += cost
            purpose = rec.get("purpose") or "turn"
            if purpose == "turn":
                s["turns"] += 1
            elif purpose == "compaction":
                s["compaction_calls"] += 1
            if rec.get("stop_reason") == "max_tokens":
                s["max_tokens_hits"] += 1
            model = rec.get("model") or "(unknown)"
            s["by_model"][model] = s["by_model"].get(model, 0.0) + cost
        elif kind == "tool":
            s["tool_calls"] += 1
            s["cost"] += _tool_record_cost(rec)
            tname = rec.get("tool_name") or "(unknown)"
            s["by_tool"][tname] = s["by_tool"].get(tname, 0) + 1

    rows = []
    for s in sessions.values():
        s["top_model"] = (
            max(s["by_model"].items(), key=lambda kv: kv[1])[0]
            if s["by_model"] else "—"
        )
        s["top_tool"] = (
            max(s["by_tool"].items(), key=lambda kv: kv[1])[0]
            if s["by_tool"] else "—"
        )
        # Duration in minutes, clamped to >= 0 so a single-record session
        # doesn't render negative when first_ts == last_ts and tz shifting
        # somehow nudges them off by a second.
        try:
            dt0 = datetime.datetime.fromisoformat(s["first_ts"])
            dt1 = datetime.datetime.fromisoformat(s["last_ts"])
            s["duration_min"] = max(0.0, (dt1 - dt0).total_seconds() / 60.0)
        except (ValueError, TypeError):
            s["duration_min"] = 0.0
        rows.append(s)
    rows.sort(key=lambda r: r["cost"], reverse=True)
    return rows


def _session_detail(all_records, session_id: str):
    """Per-session timeline + breakdowns for the session-detail route.

    Returns (header, timeline, by_model, by_tool, by_purpose, total_cost)
    where `timeline` is a chronological list of `{ts, kind, model, purpose,
    tool_name, tokens_in, tokens_out, cost, stop_reason, duration_ms,
    routed_decision}` dicts the template formats inline.
    """
    rows = [r for r in all_records if r.get("session_id") == session_id]
    rows.sort(key=lambda r: r.get("ts") or "")
    by_model: dict = {}
    by_tool: dict = {}
    by_purpose: dict = {}
    timeline = []
    total = 0.0
    user = "(anonymous)"
    first_ts = last_ts = ""
    for rec in rows:
        if rec.get("user") and user == "(anonymous)":
            user = rec["user"]
        ts = rec.get("ts") or ""
        if ts and (not first_ts or ts < first_ts):
            first_ts = ts
        if ts and (not last_ts or ts > last_ts):
            last_ts = ts
        kind = rec.get("kind")
        if kind == "api":
            cost = _report._api_cost(rec)
            total += cost
            model = rec.get("model") or "(unknown)"
            by_model[model] = by_model.get(model, 0.0) + cost
            purpose = rec.get("purpose") or "turn"
            by_purpose[purpose] = by_purpose.get(purpose, 0.0) + cost
            timeline.append({
                "ts": ts,
                "kind": "api",
                "model": model,
                "purpose": purpose,
                "tool_name": "",
                "tokens_in": rec.get("input_tokens", 0),
                "tokens_out": rec.get("output_tokens", 0),
                "cost": cost,
                "stop_reason": rec.get("stop_reason") or "",
                "duration_ms": rec.get("duration_ms"),
                "routed_decision": rec.get("routed_decision") or "",
            })
        elif kind == "tool":
            cost = _tool_record_cost(rec)
            total += cost
            tname = rec.get("tool_name") or "(unknown)"
            by_tool[tname] = by_tool.get(tname, 0.0) + cost
            timeline.append({
                "ts": ts,
                "kind": "tool",
                "model": rec.get("model") or "",
                "purpose": "",
                "tool_name": tname,
                "tokens_in": 0,
                "tokens_out": 0,
                "cost": cost,
                "stop_reason": "",
                "duration_ms": None,
                "routed_decision": "",
                "result_bytes": rec.get("result_bytes", 0) or 0,
            })
    header = {
        "session_id": session_id,
        "user": user,
        "first_ts": first_ts,
        "last_ts": last_ts,
        "record_count": len(rows),
        "api_calls": sum(1 for r in rows if r.get("kind") == "api"),
        "tool_calls": sum(1 for r in rows if r.get("kind") == "tool"),
        "turns": sum(1 for r in rows
                     if r.get("kind") == "api" and (r.get("purpose") or "turn") == "turn"),
        "compaction_calls": sum(1 for r in rows
                                if r.get("kind") == "api" and r.get("purpose") == "compaction"),
        "max_tokens_hits": sum(1 for r in rows if r.get("stop_reason") == "max_tokens"),
    }
    return header, timeline, by_model, by_tool, by_purpose, total


def _user_sessions(records, username: str):
    """All sessions belonging to one user, sorted newest first. Same row shape
    as `_aggregate_by_session` but pre-filtered."""
    own = [r for r in records
           if (r.get("user") or "(anonymous)") == username]
    rows = _aggregate_by_session(own)
    rows.sort(key=lambda r: r["last_ts"], reverse=True)
    return rows


def _user_behavior(records, username: str):
    """Per-user behavioral profile: sessions, avg msgs/session, active days,
    top tool, median $/turn, routing rate. Used by the user drill-down.

    `records` here is the user's full record list (api + tool). Day counting
    uses the timezone-adjusted `ts` so the "active days" figure matches what
    the rest of the dashboard renders.
    """
    api = [r for r in records
           if r.get("kind") == "api" and (r.get("purpose") or "turn") == "turn"]
    tools = [r for r in records if r.get("kind") == "tool"]
    sessions = {r.get("session_id") for r in records if r.get("session_id")}

    # Active days = distinct UTC-or-zone-adjusted dates with any api record.
    days = set()
    for r in records:
        if r.get("kind") == "api":
            d = str(r.get("ts", ""))[:10]
            if d:
                days.add(d)
    active_days = len(days)

    # Median $/turn: cost per "turn"-purpose call.
    turn_costs = sorted(_report._api_cost(r) for r in api)
    median_turn = _median(turn_costs) or 0.0

    # Tool mix (top 3 by call count).
    tool_counts: dict = {}
    for r in tools:
        n = r.get("tool_name") or "(unknown)"
        tool_counts[n] = tool_counts.get(n, 0) + 1
    top_tools = sorted(tool_counts.items(), key=lambda kv: kv[1], reverse=True)[:5]

    # Routing rate: among routed-decision api records, fraction Haiku.
    routed = [r for r in records
              if r.get("kind") == "api" and r.get("routed_decision")
              and (r.get("purpose") or "turn") == "turn"]
    haiku = sum(1 for r in routed if r.get("routed_decision") == "haiku")
    routing_rate = (100.0 * haiku / len(routed)) if routed else None

    msgs_per_session = (len(api) / len(sessions)) if sessions else 0.0

    # User-type badge — one short word the admin can scan.
    if active_days == 0:
        badge = "none"
    elif active_days >= 14 and msgs_per_session >= 6:
        badge = "power"
    elif active_days >= 7:
        badge = "regular"
    elif active_days >= 2:
        badge = "casual"
    else:
        badge = "one-off"

    return {
        "sessions": len(sessions),
        "msgs_per_session": msgs_per_session,
        "active_days": active_days,
        "median_turn_cost": median_turn,
        "top_tools": top_tools,
        "routing_rate_pct": routing_rate,
        "badge": badge,
    }


def _user_weekday_hour(records):
    """Per-user 7×24 weekday-hour grid — same shape as the global heatmap."""
    grid = [[0] * 24 for _ in range(7)]
    for rec in records:
        if rec.get("kind") != "api":
            continue
        try:
            ts = datetime.datetime.fromisoformat(rec["ts"])
        except (ValueError, KeyError):
            continue
        grid[ts.weekday()][ts.hour] += 1
    return grid


def _records_are_anonymized(records) -> bool:
    """True when at least one api record exists and none carry a user.

    This is the symptom of AIME_USAGE_LINK_USERS=0 in a deploy with traffic:
    every per-user view collapses to a single '(anonymous)' bucket. Surfaced
    as a banner so the admin doesn't wonder why the dashboard is empty.
    """
    has_api = False
    for rec in records:
        if rec.get("kind") == "api":
            has_api = True
            if rec.get("user"):
                return False
    return has_api


def _svg_heatmap(grid, *, width=720, height=200, row_labels=None,
                 col_labels=None, title=""):
    """Heatmap of a 2-D integer grid. ``grid[r][c]`` shades a single cell.

    Colour scale is a simple linear ramp from the background to a saturated
    blue, with the max-value cell at full saturation. Returns a ``<svg>`` plus
    optional row/column axes labels.
    """
    if not grid or not grid[0]:
        return ""
    rows = len(grid)
    cols = len(grid[0])
    pad_l, pad_t, pad_b, pad_r = 36, 18, 22, 8
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    cw = plot_w / cols
    ch = plot_h / rows

    hi = 0
    for row in grid:
        for v in row:
            if v > hi:
                hi = v
    if hi == 0:
        hi = 1

    rects = []
    for r in range(rows):
        for c in range(cols):
            v = grid[r][c]
            frac = v / hi
            # interpolate from #8881 (idle) to #2f6fd0 (peak)
            if v == 0:
                fill = "#8881"
            else:
                # Linear ramp in HSL would be nicer, but a single-stop alpha
                # over the brand blue keeps the SVG self-contained.
                alpha = max(0.18, frac)
                fill = f"rgba(47,111,208,{alpha:.2f})"
            x = pad_l + c * cw
            y = pad_t + r * ch
            label = (row_labels[r] if row_labels else str(r))
            col_lbl = (col_labels[c] if col_labels else str(c))
            rects.append(
                f'<rect x="{x:.1f}" y="{y:.1f}" width="{cw - 1:.1f}" '
                f'height="{ch - 1:.1f}" fill="{fill}" rx="2">'
                f'<title>{label} {col_lbl} — {v:,} call{"s" if v != 1 else ""}</title>'
                f'</rect>'
            )

    row_lbls = []
    if row_labels:
        for r in range(rows):
            y = pad_t + r * ch + ch / 2 + 3
            row_lbls.append(
                f'<text x="{pad_l - 6}" y="{y:.1f}" font-size="10" '
                f'text-anchor="end" fill="#888">{row_labels[r]}</text>'
            )
    col_lbls = []
    if col_labels:
        step = max(1, cols // 12)
        for c in range(0, cols, step):
            x = pad_l + c * cw + cw / 2
            col_lbls.append(
                f'<text x="{x:.1f}" y="{height - 6}" font-size="10" '
                f'text-anchor="middle" fill="#888">{col_labels[c]}</text>'
            )

    title_svg = (
        f'<text x="{pad_l}" y="12" font-size="10" fill="#888">{title}</text>'
        if title else ""
    )

    return (
        f'<svg class="chart" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">'
        + title_svg + "".join(rects) + "".join(row_lbls) + "".join(col_lbls)
        + "</svg>"
    )


def _recent_records(records, n=25, user=None):
    """The N most-recent API records, optionally filtered to one user."""
    out = []
    for rec in records:
        if rec.get("kind") != "api":
            continue
        if user is not None and (rec.get("user") or "(anonymous)") != user:
            continue
        out.append(rec)
    out.sort(key=lambda r: r.get("ts", ""), reverse=True)
    return out[:n]


# ---------------------------------------------------------------------------
# SVG chart helpers
#
# All charts are server-rendered inline SVG — no client-side JS or external
# libraries. Each helper returns a string that the templates drop in with
# ``{{ ... |safe }}``. Polylines / rects are sized to the requested viewBox
# and scaled to the data's own min/max so a low-traffic day on a quiet log
# still draws a chart that fills the box.
# ---------------------------------------------------------------------------


def _svg_sparkline(values, width=120, height=28, color="#2f6fd0"):
    """Tiny line with no axes — meant to live inside a table cell.

    Empty / all-zero input returns a dim flat line so the table column keeps
    its visual alignment instead of collapsing to nothing.
    """
    if not values:
        return (f'<svg class="sparkline" width="{width}" height="{height}" '
                f'viewBox="0 0 {width} {height}"></svg>')
    hi = max(values)
    lo = min(values)
    span = (hi - lo) or 1.0
    n = len(values)
    if n == 1:
        x = width / 2
        y = height / 2
        return (f'<svg class="sparkline" width="{width}" height="{height}" '
                f'viewBox="0 0 {width} {height}">'
                f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2" fill="{color}"/></svg>')
    pts = []
    for i, v in enumerate(values):
        x = (i / (n - 1)) * width
        y = height - ((v - lo) / span) * (height - 2) - 1
        pts.append(f"{x:.1f},{y:.1f}")
    fill_pts = pts + [f"{width:.1f},{height:.1f}", f"0,{height:.1f}"]
    return (
        f'<svg class="sparkline" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">'
        f'<polygon points="{" ".join(fill_pts)}" fill="{color}" opacity="0.15"/>'
        f'<polyline points="{" ".join(pts)}" fill="none" stroke="{color}" '
        f'stroke-width="1.4"/>'
        f"</svg>"
    )


def _svg_line_chart(day_keys, series, *, width=720, height=220, y_label="",
                    money=False, colors=None):
    """Multi-series line chart with axes, gridlines, and a small legend.

    ``series`` is a list of ``(name, values_aligned_to_day_keys)`` — each
    series can carry ``None`` to leave that day disconnected. The y axis is
    auto-scaled to the data; the x axis labels the first, middle, and last
    day_keys (more would overlap in a normal-width admin viewport).
    """
    colors = colors or ["#2f6fd0", "#2e9e4f", "#8a4fd0", "#c8860a", "#d23",
                        "#4d8bd6", "#6fbf85", "#a978d8", "#dca345", "#e36d6d"]
    if not day_keys or not series:
        return (f'<svg class="chart" width="{width}" height="{height}" '
                f'viewBox="0 0 {width} {height}"></svg>')

    pad_l, pad_r, pad_t, pad_b = 50, 12, 14, 30
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b

    flat = [v for _name, vs in series for v in vs if v is not None]
    if not flat:
        flat = [0.0]
    hi = max(flat)
    lo = min(0.0, min(flat))
    if hi == lo:
        hi = lo + 1.0
    span = hi - lo

    def _x(i, n):
        return pad_l + (i / max(1, n - 1)) * plot_w

    def _y(v):
        return pad_t + plot_h - ((v - lo) / span) * plot_h

    n = len(day_keys)

    # Gridlines + y labels.
    grid = []
    for frac in (0, 0.25, 0.5, 0.75, 1.0):
        y_val = lo + frac * span
        y = pad_t + plot_h - frac * plot_h
        label = f"${y_val:,.2f}" if money else f"{y_val:,.0f}"
        grid.append(
            f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width - pad_r}" '
            f'y2="{y:.1f}" stroke="#8884" stroke-width="0.5"/>'
            f'<text x="{pad_l - 6}" y="{y + 3:.1f}" font-size="10" '
            f'text-anchor="end" fill="#888">{label}</text>'
        )

    # X labels: first, middle, last (avoids overlap at chart widths around 700).
    x_labels = []
    label_idxs = (
        {0} if n == 1 else {0, n // 2, n - 1}
    )
    for i in sorted(label_idxs):
        x_labels.append(
            f'<text x="{_x(i, n):.1f}" y="{height - 8}" font-size="10" '
            f'text-anchor="middle" fill="#888">{day_keys[i][5:]}</text>'
        )

    # Polylines per series, skipping None gaps as separate segments.
    polylines = []
    legend = []
    for s_i, (name, values) in enumerate(series):
        col = colors[s_i % len(colors)]
        segs = []
        current = []
        for i, v in enumerate(values):
            if v is None:
                if current:
                    segs.append(current)
                    current = []
            else:
                current.append((i, v))
        if current:
            segs.append(current)
        for seg in segs:
            if len(seg) == 1:
                i, v = seg[0]
                polylines.append(
                    f'<circle cx="{_x(i, n):.1f}" cy="{_y(v):.1f}" r="2" '
                    f'fill="{col}"/>'
                )
            else:
                pts = " ".join(f"{_x(i, n):.1f},{_y(v):.1f}" for i, v in seg)
                polylines.append(
                    f'<polyline points="{pts}" fill="none" stroke="{col}" '
                    f'stroke-width="1.6"/>'
                )
        legend.append(
            f'<span class="lk"><span class="sw" style="background:{col}"></span>{name}</span>'
        )

    title = (
        f'<text x="{pad_l}" y="10" font-size="10" fill="#888">{y_label}</text>'
        if y_label else ""
    )

    svg = (
        f'<svg class="chart" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">'
        + title + "".join(grid) + "".join(x_labels) + "".join(polylines)
        + "</svg>"
    )
    return (
        f'<div class="chart-wrap">{svg}'
        f'<div class="legend">{"".join(legend)}</div></div>'
    )


def _svg_stacked_bars(day_keys, series, *, width=720, height=220, money=False):
    """Stacked vertical bars — one stack per day, ordered top-down by series.

    The first ``series`` entry is drawn at the bottom of each stack, matching
    "biggest contributor at the base". Returns an empty svg shell for empty
    input so the layout doesn't collapse.
    """
    colors = ["#2f6fd0", "#2e9e4f", "#8a4fd0", "#c8860a", "#d23",
              "#4d8bd6", "#6fbf85", "#a978d8", "#dca345", "#e36d6d"]
    if not day_keys or not series:
        return (f'<svg class="chart" width="{width}" height="{height}" '
                f'viewBox="0 0 {width} {height}"></svg>')

    pad_l, pad_r, pad_t, pad_b = 50, 12, 14, 30
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    n = len(day_keys)
    totals = [0.0] * n
    for _name, vs in series:
        for i, v in enumerate(vs):
            totals[i] += v
    hi = max(totals) if totals else 1.0
    if hi == 0:
        hi = 1.0

    bar_w = (plot_w / n) * 0.78
    gap = (plot_w / n) - bar_w

    def _x_left(i):
        return pad_l + i * (bar_w + gap) + gap / 2

    def _h(v):
        return (v / hi) * plot_h

    grid = []
    for frac in (0, 0.5, 1.0):
        y_val = frac * hi
        y = pad_t + plot_h - frac * plot_h
        label = f"${y_val:,.2f}" if money else f"{y_val:,.0f}"
        grid.append(
            f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width - pad_r}" '
            f'y2="{y:.1f}" stroke="#8884" stroke-width="0.5"/>'
            f'<text x="{pad_l - 6}" y="{y + 3:.1f}" font-size="10" '
            f'text-anchor="end" fill="#888">{label}</text>'
        )

    bars = []
    base = [pad_t + plot_h] * n
    legend = []
    for s_i, (name, vs) in enumerate(series):
        col = colors[s_i % len(colors)]
        for i, v in enumerate(vs):
            h = _h(v)
            if h <= 0:
                continue
            y = base[i] - h
            bars.append(
                f'<rect x="{_x_left(i):.1f}" y="{y:.1f}" width="{bar_w:.1f}" '
                f'height="{h:.1f}" fill="{col}"><title>{day_keys[i]} {name}: '
                f'{("$" + format(v, ",.4f")) if money else format(v, ",.0f")}</title></rect>'
            )
            base[i] -= h
        legend.append(
            f'<span class="lk"><span class="sw" style="background:{col}"></span>{name}</span>'
        )

    label_idxs = {0} if n == 1 else {0, n // 2, n - 1}
    x_labels = []
    for i in sorted(label_idxs):
        x_labels.append(
            f'<text x="{(_x_left(i) + bar_w / 2):.1f}" y="{height - 8}" '
            f'font-size="10" text-anchor="middle" fill="#888">{day_keys[i][5:]}</text>'
        )

    svg = (
        f'<svg class="chart" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">'
        + "".join(grid) + "".join(bars) + "".join(x_labels) + "</svg>"
    )
    return (
        f'<div class="chart-wrap">{svg}'
        f'<div class="legend">{"".join(legend)}</div></div>'
    )


def _svg_hour_bars(hours, *, width=720, height=120, tz_label="UTC"):
    """A wider, labelled replacement for the inline-CSS hourly bar block."""
    if not hours:
        return ""
    pad_l, pad_r, pad_t, pad_b = 30, 12, 6, 22
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    hi = max(hours) or 1
    bar_w = (plot_w / 24) * 0.78
    gap = (plot_w / 24) - bar_w

    rects = []
    for i, c in enumerate(hours):
        h = (c / hi) * plot_h
        x = pad_l + i * (bar_w + gap) + gap / 2
        y = pad_t + plot_h - h
        col = "#2f6fd0" if c else "#8882"
        rects.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" '
            f'height="{max(1, h):.1f}" fill="{col}" rx="1"><title>'
            f'{i:02d}:00 {tz_label} — {c:,} call{"s" if c != 1 else ""}</title></rect>'
        )

    labels = []
    for i in range(0, 24, 3):
        x = pad_l + i * (bar_w + gap) + gap / 2 + bar_w / 2
        labels.append(
            f'<text x="{x:.1f}" y="{height - 6}" font-size="10" '
            f'text-anchor="middle" fill="#888">{i:02d}</text>'
        )

    return (
        f'<svg class="chart" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">'
        + "".join(rects) + "".join(labels) + "</svg>"
    )


def _svg_donut(slices, *, size=160, inner=0.55):
    """Donut chart for a categorical mix (e.g., per-model cost share).

    ``slices`` is ``[(name, value, color), ...]``. Slices below ~2% of the
    total are still drawn but skip their label to avoid overlap. Returns an
    inline ``<svg>`` plus a sibling legend.
    """
    if not slices or sum(s[1] for s in slices) <= 0:
        return f'<div class="donut-wrap"><svg width="{size}" height="{size}"></svg></div>'
    total = sum(s[1] for s in slices)
    cx, cy = size / 2, size / 2
    r_o = size / 2 - 2
    r_i = r_o * inner

    import math
    paths = []
    angle = -math.pi / 2
    for name, value, color in slices:
        if value <= 0:
            continue
        frac = value / total
        end = angle + frac * 2 * math.pi
        large = 1 if frac > 0.5 else 0
        x1 = cx + r_o * math.cos(angle)
        y1 = cy + r_o * math.sin(angle)
        x2 = cx + r_o * math.cos(end)
        y2 = cy + r_o * math.sin(end)
        x3 = cx + r_i * math.cos(end)
        y3 = cy + r_i * math.sin(end)
        x4 = cx + r_i * math.cos(angle)
        y4 = cy + r_i * math.sin(angle)
        d = (
            f"M {x1:.2f} {y1:.2f} "
            f"A {r_o:.2f} {r_o:.2f} 0 {large} 1 {x2:.2f} {y2:.2f} "
            f"L {x3:.2f} {y3:.2f} "
            f"A {r_i:.2f} {r_i:.2f} 0 {large} 0 {x4:.2f} {y4:.2f} Z"
        )
        paths.append(
            f'<path d="{d}" fill="{color}"><title>{name}: '
            f'{100*frac:.1f}%</title></path>'
        )
        angle = end

    legend = "".join(
        f'<div class="lk"><span class="sw" style="background:{c}"></span>'
        f'{n} <span class="note">({100*v/total:.1f}%)</span></div>'
        for n, v, c in slices if v > 0
    )
    svg = (
        f'<svg width="{size}" height="{size}" viewBox="0 0 {size} {size}">'
        + "".join(paths) + "</svg>"
    )
    return f'<div class="donut-wrap">{svg}<div class="legend col">{legend}</div></div>'


_PALETTE = ("#2f6fd0", "#2e9e4f", "#8a4fd0", "#c8860a", "#d23",
            "#4d8bd6", "#6fbf85", "#a978d8", "#dca345", "#e36d6d")


def _color_for(i):
    """Stable colour from the palette for stable indexing in templates."""
    return _PALETTE[i % len(_PALETTE)]


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------


def _csrf_token() -> str:
    """The current session's CSRF token, minting one on first use. Embedded in
    every state-changing form and checked on the matching POST."""
    token = session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(24)
        session["csrf"] = token
    return token


def _check_csrf() -> bool:
    """True if the submitted form carries this session's CSRF token."""
    sent = request.form.get("csrf", "")
    have = session.get("csrf", "")
    return bool(have) and secrets.compare_digest(sent, have)


def _flash(level: str, msg: str) -> None:
    """Queue a one-shot message (level: ok / warn / bad) for the next page."""
    queued = session.get("flash", [])
    queued.append({"level": level, "msg": msg})
    session["flash"] = queued


def admin_required(view):
    """Gate a view behind a logged-in admin session."""
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not session.get("admin"):
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapper


def admin_post(view):
    """Gate a state-changing POST: admin session + a valid CSRF token. A
    failed CSRF check is dropped with a flash rather than executed."""
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not session.get("admin"):
            return redirect(url_for("login"))
        if not _check_csrf():
            _flash("bad", "Security check failed — action ignored. Try again.")
            return redirect(url_for("index", tab="accounts"))
        return view(*args, **kwargs)
    return wrapper


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

_FRAGMENT_OVERVIEW = """<div class="meta">
    <span title="Filesystem path of the usage.jsonl log being read.">log: {{ log }}</span><br>
    <span title="Number of log records matching the current filters.">{{ record_count }} records</span>
    &middot;
    <span title="Date range currently in view, set by the Since / Until filters above.">window: {{ window }}</span>
    {% if delta %} &middot;
    <span title="Equal-length window immediately before the current one — used by the period-over-period deltas on the cards.">vs prior {{ prev_window }}</span>
    {% endif %}
  </div>

  {% for e in errors %}<p class="err">{{ e }}</p>{% endfor %}

  {% if anonymized %}
  <div class="banner warn">
    <strong>User attribution is disabled.</strong>
    Every per-user view collapses to a single "(anonymous)" bucket because
    <code>AIME_USAGE_LINK_USERS=1</code> is not set. Aggregate totals still
    work; per-user and per-conversation breakdowns won't.
  </div>
  {% endif %}

  {% if not users %}
    <p class="empty">No usage recorded in this window. Collection is enabled
    with AIME_USAGE_STATS=1.</p>
  {% else %}

  <div class="view-toggle"
    title="Switch the headline cards between window totals and the per-user average. Deltas compare against the same prior window.">
    <span class="lbl">Show:</span>
    <a href="/?{{ qs_view_total }}" class="{{ 'active' if view == 'total' else '' }}"
      title="Totals across every user active in this window.">Total</a>
    <a href="/?{{ qs_view_avg }}" class="{{ 'active' if view == 'avg' else '' }}"
      title="Divide $ / call / token figures by the {{ user_count }} user(s) active in this window.">Avg / user</a>
    <span class="note">({{ user_count }} user{{ '' if user_count == 1 else 's' }} active in this window)</span>
  </div>

  <div class="cards">
    <div class="card accent-green"
      title="Total billed cost in this window: input + output + cache + web-search charges. An estimate from list prices, not the literal invoice.">
      <div class="num good">${{ "%.4f"|format(card_cost) }}</div>
      <div class="lbl">{{ 'avg $ / user' if view == 'avg' else '$ spent' }}</div>
      {% if delta and view == 'total' %}
        {% if delta.cost is none %}
          <div class="delta flat">no prior baseline</div>
        {% else %}
          <div class="delta {{ 'up' if delta.cost > 0 else 'down' if delta.cost < 0 else 'flat' }}"
            title="Cost in the prior equal-length window was ${{ '%.4f'|format(prev.cost) }}.">
            {{ "%+.1f"|format(delta.cost) }}% vs prior
          </div>
        {% endif %}
      {% endif %}
    </div>
    <div class="card accent-blue"
      title="API calls in this window.">
      <div class="num blue">{{ ('%.1f' % card_calls) if view == 'avg' else '{:,}'.format(card_calls) }}</div>
      <div class="lbl">{{ 'avg API calls / user' if view == 'avg' else 'API calls' }}</div>
      {% if delta and view == 'total' %}
        {% if delta.calls is none %}
          <div class="delta flat">no prior baseline</div>
        {% else %}
          <div class="delta {{ 'up' if delta.calls > 0 else 'down' if delta.calls < 0 else 'flat' }}"
            title="Prior window: {{ '{:,}'.format(prev.calls) }} calls.">
            {{ "%+.1f"|format(delta.calls) }}% vs prior
          </div>
        {% endif %}
      {% endif %}
    </div>
    <div class="card accent-purple"
      title="Distinct users with at least one API record in this window.">
      <div class="num purple">{{ user_count }}</div>
      <div class="lbl">active users</div>
      {% if delta and view == 'total' %}
        {% if delta.users is none %}
          <div class="delta flat">no prior baseline</div>
        {% else %}
          <div class="delta {{ 'up' if delta.users > 0 else 'down' if delta.users < 0 else 'flat' }}"
            title="Prior window: {{ prev.users }} active users.">
            {{ "%+.1f"|format(delta.users) }}% vs prior
          </div>
        {% endif %}
      {% endif %}
    </div>
    <div class="card {{ 'accent-green' if cache_hit_pct >= 70 else 'accent-amber' if cache_hit_pct >= 40 else 'accent-red' }}"
      title="Share of read-side prompt tokens served from cache rather than billed as fresh input. Higher is cheaper. Green ≥70%, amber 40–70%, red <40%.">
      <div class="num {{ 'good' if cache_hit_pct >= 70 else 'warn' if cache_hit_pct >= 40 else 'bad' }}">{{ "%.0f"|format(cache_hit_pct) }}%</div>
      <div class="lbl">cache hit rate</div></div>
  </div>

  <h2 title="Estimated USD cost charged per UTC day in this window, with a 7-day rolling average overlay so a single spike doesn't dominate the trend read.">Cost over time</h2>
  {{ chart_daily_cost_smoothed|safe }}

  <h2 title="Daily cost split by model id, stacked. The widest band is the model carrying the most spend.">Cost by model (daily)</h2>
  {{ chart_model_stack|safe }}

  <div class="two-col">
    <div>
      <h2 title="API request volume and the number of distinct users active per UTC day.">Activity volume</h2>
      {{ chart_daily_calls|safe }}
    </div>
    <div>
      <h2 title="Share of spend per model across the whole window.">Model mix</h2>
      {{ chart_model_donut|safe }}
    </div>
  </div>

  <h2 title="One row per user with any activity in the window — click a username for behavior, conversations, and tools. Token plumbing lives on the drill-down.">By user</h2>
  <table>
    <thead>
      <tr>
        <th title="Username. Click to open the per-user drill-down.">User</th>
        <th title="Daily cost trend across the visible window.">Trend</th>
        <th title="Profile badge derived from the user's lifetime activity: power / regular / casual / one-off.">Profile</th>
        <th title="Distinct conversations (session_ids) this user had in the window.">Conv.</th>
        <th title="Distinct calendar days this user appeared on within the window.">Active days</th>
        <th title="API calls in the window.">API calls</th>
        <th title="Median billed cost of an assistant turn — distinguishes light Q&A users from heavy reasoning ones.">$/turn</th>
        <th title="Billed cost in this window: input + output + cache + web-search.">$ spent</th>
      </tr>
    </thead>
    <tbody>
      {% for name, u in users %}
      <tr>
        <td><a href="/user/{{ name|urlencode }}" class="userlink"
              title="Open {{ name }}'s drill-down.">{{ name }}</a></td>
        <td class="spark">{{ user_sparklines.get(name, '')|safe }}</td>
        <td>
          {% set b = user_badges.get(name, 'none') %}
          <span class="badge badge-{{ b }}">{{ b }}</span>
        </td>
        <td>{{ user_session_counts.get(name, 0) }}</td>
        <td>{{ user_active_days.get(name, 0) }}</td>
        <td>{{ "{:,}".format(u.api_calls) }}</td>
        <td>${{ "%.5f"|format(user_median_turn.get(name, 0)) }}</td>
        <td class="cost good">${{ "%.4f"|format(u.cost) }}</td>
      </tr>
      {% endfor %}
    </tbody>
    <tfoot>
      <tr>
        <td>Total</td>
        <td colspan="6"></td>
        <td class="cost good">${{ "%.4f"|format(grand_cost) }}</td>
      </tr>
    </tfoot>
  </table>

  <h2 title="Rich per-day summary across the visible window, newest first. Combines the old 'by day' table with active-user counts, p50 latency, cache hit %, and the most expensive model / user that day.">Daily summary</h2>
  {% if not daily_rows %}
    <p class="empty">No daily activity in this window.</p>
  {% else %}
  <table>
    <thead>
      <tr>
        <th title="UTC calendar date.">Date</th>
        <th title="API calls on this date.">Calls</th>
        <th title="Distinct users active on this date.">Users</th>
        <th title="Estimated USD cost on this date.">Cost</th>
        <th title="Mean USD cost per call on this date — a high value means heavy turns.">$/call</th>
        <th title="Median wall-clock latency on this date (records that carry duration_ms).">p50 ms</th>
        <th title="Share of read-side prompt tokens served from cache rather than billed fresh.">Cache hit</th>
        <th title="Model with the most cost attributed on this date.">Top model</th>
        <th title="User with the most cost attributed on this date.">Top user</th>
      </tr>
    </thead>
    <tbody>
      {% for r in daily_rows|reverse %}
      <tr>
        <td>{{ r.day }}</td>
        <td>{{ "{:,}".format(r.calls) }}</td>
        <td>{{ r.active_users }}</td>
        <td class="cost good">${{ "%.4f"|format(r.cost) }}</td>
        <td>${{ "%.5f"|format(r.cost_per_call) }}</td>
        <td>{{ ('%.0f' % r.p50) if r.p50 is not none else '—' }}</td>
        <td class="{{ 'good' if r.cache_hit_pct >= 70 else 'warn' if r.cache_hit_pct >= 40 else 'bad' }}">{{ '%.0f'|format(r.cache_hit_pct) }}%</td>
        <td>{{ r.top_model }}</td>
        <td><a href="/user/{{ r.top_user|urlencode }}" class="userlink">{{ r.top_user }}</a></td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
  {% endif %}

  <h2 title="API token cost grouped by the model id stamped on each record, most expensive first.">By model</h2>
  <table>
    <thead>
      <tr>
        <th title="Model id the API stamped on the record (e.g. claude-sonnet-4-6). (unknown) means the record carried no model.">Model</th>
        <th title="Per-day cost trend for this model across the visible window.">Trend</th>
        <th title="Requests served by this model.">API calls</th>
        <th title="Fresh, uncached input tokens sent to this model.">Input</th>
        <th title="Tokens generated by this model.">Output</th>
        <th title="Estimated USD cost attributed to this model.">Est. cost</th>
      </tr>
    </thead>
    <tbody>
      {% for name, m in by_model %}
      <tr>
        <td>{{ name }}</td>
        <td class="spark">{{ model_sparklines.get(name, '')|safe }}</td>
        <td>{{ "{:,}".format(m.api_calls) }}</td>
        <td>{{ "{:,}".format(m.input) }}</td>
        <td>{{ "{:,}".format(m.output) }}</td>
        <td class="cost good">${{ "%.4f"|format(m.cost) }}</td>
      </tr>
      {% endfor %}
    </tbody>
  </table>

  <h2 title="How well each usage tier fits its users: how much of the daily allowance they consume, and how many exceed it, how often. Drives tier sizing — see docs/usage-limits.md.">Tiers</h2>
  {% if anonymized %}
  <p class="note">User linkage is off (AIME_USAGE_LINK_USERS=0), so spend can't be attributed to a user or tier. Turn it on to populate this section.</p>
  {% endif %}
  {% if not tier_rows %}
  <p class="empty">No tier data in this window.</p>
  {% else %}
  <table>
    <thead>
      <tr>
        <th title="Usage-limit tier. '(unknown)' is spend from accounts not currently on a known tier (e.g. since-deleted).">Tier</th>
        <th title="Daily cost allowance for this tier.">Daily cap</th>
        <th title="Accounts currently on this tier.">Users</th>
        <th title="Users on this tier with any spend in the window.">Active</th>
        <th title="Average cost per active user-day.">Avg $/day</th>
        <th title="Average daily spend as a percent of the tier's daily allowance. Over 100% means the typical active day exceeds the cap — the tier may be undersized.">Avg utilization</th>
        <th title="Distinct users who exceeded the daily allowance on at least one day in the window.">Users over</th>
        <th title="Active user-days that exceeded the daily allowance, out of all active user-days (and the rate). Note the bucket banks several days, so a day over the cap is not necessarily a blocked day.">Days over</th>
        <th title="Most expensive single user-day on this tier.">Peak day</th>
      </tr>
    </thead>
    <tbody>
      {% for t in tier_rows %}
      <tr>
        <td>{{ t.tier }}</td>
        <td>{% if t.cap %}${{ "%.2f"|format(t.cap) }}{% else %}—{% endif %}</td>
        <td>{{ t.n_users }}</td>
        <td>{{ t.n_active }}</td>
        <td class="cost">${{ "%.4f"|format(t.avg_daily) }}</td>
        <td class="{{ 'bad' if t.avg_util_pct > 100 else ('good' if t.avg_util_pct else '') }}">{{ t.avg_util_pct|round|int }}%</td>
        <td class="{{ 'bad' if t.users_over else '' }}">{{ t.users_over }}</td>
        <td>{% if t.user_days %}{{ t.over_days }} / {{ t.user_days }} ({{ t.over_rate_pct|round|int }}%){% else %}—{% endif %}</td>
        <td class="cost">${{ "%.2f"|format(t.max_day) }}</td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
  <p class="note">"Over" compares each user's daily spend to their tier's daily allowance. Tier reflects each account's current tier; mid-window changes aren't back-applied.</p>
  {% endif %}
  {% endif %}"""


_FRAGMENT_CACHE = """<div class="meta">
    <span title="Filesystem path of the usage.jsonl log being read. Resolved from AIME_DATABASE_DIR, identical to the CLI usage report.">log: {{ log }}</span><br>
    <span title="Number of log records matching the current filters.">{{ record_count }} records</span>
    &middot;
    <span title="Date range currently in view, set by the Since / Until filters above.">window: {{ window }}</span>
  </div>

  {% for e in errors %}<p class="err">{{ e }}</p>{% endfor %}

  {% if not cache_users %}
    <p class="empty">No API records in this window — nothing to analyse.</p>
  {% else %}

  {% if cache_savings >= 0 %}
  <div class="banner good">Prompt caching saved an estimated
    ${{ "%.4f"|format(cache_savings) }}
    ({{ "%.0f"|format(cache_savings_pct) }}%) in this window.</div>
  {% else %}
  <div class="banner bad">Prompt caching cost an estimated
    ${{ "%.4f"|format(-cache_savings) }} extra in this window — the write
    premium is outrunning the read discount. See flagged users below.</div>
  {% endif %}

  <div class="cards">
    <div class="card accent-blue" title="Prompt-side cost with caching on — only counts input/cache-read/cache-write tokens at their actual billed rates. Output tokens and web-search are excluded because they are identical with or without caching, so this is a clean comparison rather than a total. For total spend, see Overview.">
      <div class="num blue">${{ "%.4f"|format(cache_with) }}</div>
      <div class="lbl">prompt cost (cached)</div></div>
    <div class="card accent-purple" title="Hypothetical prompt-side cost if caching were off — every cache read and write re-billed as plain input at 1x. Output and web-search excluded for apples-to-apples with the 'cached' card.">
      <div class="num purple">${{ "%.4f"|format(cache_without) }}</div>
      <div class="lbl">prompt cost (no cache)</div></div>
    <div class="card {{ 'accent-green' if cache_savings >= 0 else 'accent-red' }}"
      title="No-cache cost minus actual cost. Positive (green) means caching saved money; negative (red) means the write premium outran the read discount.">
      <div class="num {{ 'good' if cache_savings >= 0 else 'bad' }}">${{ "%.4f"|format(cache_savings) }}</div>
      <div class="lbl">net savings</div></div>
    <div class="card {{ 'accent-green' if cache_reuse >= 3 else 'accent-amber' if cache_reuse >= 1 else 'accent-red' }}"
      title="Cache-read tokens divided by cache-write tokens — how many times the average cached segment is read back. Green at 3x+, amber 1-3x, red below 1x (writes not recouped).">
      <div class="num {{ 'good' if cache_reuse >= 3 else 'warn' if cache_reuse >= 1 else 'bad' }}">{{ "%.2f"|format(cache_reuse) }}&times;</div>
      <div class="lbl">cache reuse factor</div></div>
  </div>

  <h2 title="Daily prompt-side cache savings (no-cache cost minus actual cost) in USD. Positive bars are days where caching paid for itself; negative bars are days where the write premium outran the read discount.">Cache savings over time</h2>
  {{ chart_cache_savings|safe }}

  {% if flagged %}
  <div class="banner warn">5-minute-TTL warning: {{ flagged|join(', ') }}
    {{ 'averages' if flagged|length == 1 else 'average' }} more than 5 minutes
    between requests. A 5m cache write likely expires before it is read back,
    so each turn re-pays the write premium for no read discount. A 1h-TTL
    write would survive the gap.</div>
  {% endif %}

  <p class="note">Reuse factor = cache-read tokens &divide; cache-write tokens
    (how many times the average cached segment is read back). A write is only
    worth its premium once reads recoup it: <span class="good">&ge;3&times; healthy</span>,
    <span class="warn">1&ndash;3&times; marginal</span>,
    <span class="bad">&lt;1&times; losing money</span>.</p>

  <h2 title="Cache economics per user, heaviest no-cache cost first.">By user</h2>
  <table>
    <thead>
      <tr>
        <th title="Username the records were logged under. (anonymous) covers records with no username.">User</th>
        <th title="Requests sent to the Anthropic Messages API by this user.">API calls</th>
        <th title="Tokens written into the prompt cache, split by TTL. 5m write costs 1.25x base input, 1h write 2x.">Cache wr (5m/1h)</th>
        <th title="Tokens read back from the prompt cache, billed at 0.10x base input.">Cache rd</th>
        <th title="Cache reads divided by cache writes. Above 1x the cache is recouping its write premium; below 1x it is losing money.">Reuse</th>
        <th title="Cache-write tokens not covered by an equal number of reads (writes minus reads, floored at 0). A lower bound on write premium paid for no read discount.">Unread writes</th>
        <th title="Median time between this user's consecutive API requests. Above 5 minutes, a 5m-TTL cache write tends to expire before it is read back (row turns red with a warning sign).">Median gap</th>
        <th title="Prompt-side cost with caching on (input + cache reads + cache writes). Output and web-search excluded — they are identical with or without caching, so this is a clean comparison column.">Prompt $ (cached)</th>
        <th title="Hypothetical prompt-side cost if caching were off — every cache read/write re-billed as plain input.">Prompt $ (no cache)</th>
        <th title="No-cache cost minus actual cost for this user. Positive = caching saved money.">Savings</th>
      </tr>
    </thead>
    <tbody>
      {% for name, u in cache_users %}
      <tr>
        <td>{{ name }}</td>
        <td>{{ "{:,}".format(u.calls) }}</td>
        <td>{{ "{:,}".format(u.w5m) }} / {{ "{:,}".format(u.w1h) }}</td>
        <td>{{ "{:,}".format(u.reads) }}</td>
        <td class="{{ 'good' if u.reuse >= 3 else 'warn' if u.reuse >= 1 else 'bad' }}">{{ "%.2f"|format(u.reuse) }}&times;</td>
        <td class="{{ 'bad' if u.unread_writes > 0 else '' }}">{{ "{:,}".format(u.unread_writes) }}</td>
        <td class="{{ 'bad' if u.ttl_risk else '' }}">
          {%- if u.median_gap is none -%}&mdash;
          {%- else -%}{{ "%.1f"|format(u.median_gap / 60.0) }}m{%- endif -%}
          {%- if u.ttl_risk %} &#9888;{% endif -%}
        </td>
        <td class="cost blue">${{ "%.4f"|format(u.with_cache) }}</td>
        <td class="cost purple">${{ "%.4f"|format(u.without_cache) }}</td>
        <td class="cost {{ 'good' if u.savings >= 0 else 'bad' }}">
          ${{ "%.4f"|format(u.savings) }}
          ({{ "%+.0f"|format(u.savings_pct) }}%)</td>
      </tr>
      {% endfor %}
    </tbody>
    <tfoot>
      <tr>
        <td>Total</td><td colspan="6"></td>
        <td class="cost blue">${{ "%.4f"|format(cache_with) }}</td>
        <td class="cost purple">${{ "%.4f"|format(cache_without) }}</td>
        <td class="cost {{ 'good' if cache_savings >= 0 else 'bad' }}">${{ "%.4f"|format(cache_savings) }}</td>
      </tr>
    </tfoot>
  </table>
  {% endif %}"""


# Activity tab — what the API is being *used* for, beyond the raw cost on
# Overview: purpose mix (turn vs. background plumbing), stop-reason
# distribution (truncation / tool-use rates), latency percentiles, and
# UTC-hour traffic shape. Filters share the form with Overview / Cache.
_FRAGMENT_ACTIVITY = """<div class="meta">
    <span title="Filesystem path of the usage.jsonl log being read. Resolved from AIME_DATABASE_DIR, identical to the CLI usage report.">log: {{ log }}</span><br>
    <span title="Number of log records matching the current filters.">{{ record_count }} records</span>
    &middot;
    <span title="Date range currently in view, set by the Since / Until filters above.">window: {{ window }}</span>
  </div>

  {% for e in errors %}<p class="err">{{ e }}</p>{% endfor %}

  {% if not purpose_rows %}
    <p class="empty">No API records in this window — nothing to analyse.</p>
  {% else %}

  <div class="cards">
    <div class="card accent-blue"
      title="Median wall-clock latency of an API call (records that carry a duration_ms — newer records do, older ones may not). Half of calls finish faster than this.">
      <div class="num blue">{{ ('%.0f ms' % lat_p50) if lat_p50 is not none else '—' }}</div>
      <div class="lbl">latency p50</div></div>
    <div class="card accent-amber"
      title="90th-percentile latency. One call in ten takes at least this long. A growing p90 is the usual early sign of a slow tail.">
      <div class="num warn">{{ ('%.0f ms' % lat_p90) if lat_p90 is not none else '—' }}</div>
      <div class="lbl">latency p90</div></div>
    <div class="card accent-red"
      title="99th-percentile latency. The slowest 1% of calls. Watch this against a service-level target.">
      <div class="num bad">{{ ('%.0f ms' % lat_p99) if lat_p99 is not none else '—' }}</div>
      <div class="lbl">latency p99</div></div>
    <div class="card accent-purple"
      title="Mean (arithmetic average) of duration_ms across {{ '{:,}'.format(lat_n) }} record(s) that carry a latency. Pulled higher than p50 by the long tail — keep an eye on the percentiles for the real shape.">
      <div class="num purple">{{ ('%.0f ms' % lat_avg) if lat_avg is not none else '—' }}</div>
      <div class="lbl">latency avg</div></div>
  </div>

  <h2 title="API calls split by their `purpose` tag — 'turn' is a user-facing assistant turn, 'title' / 'compaction' are background Haiku jobs the user never sees directly. A high background share means a lot of the bill goes to plumbing.">By purpose</h2>
  <table>
    <thead>
      <tr>
        <th title="Purpose tag stamped on the record by aime.usage.record_api.">Purpose</th>
        <th title="Number of API calls with this purpose.">Calls</th>
        <th title="Fresh input tokens.">Input</th>
        <th title="Output tokens generated.">Output</th>
        <th title="Median latency (ms) across calls of this purpose that carry a duration_ms.">p50 (ms)</th>
        <th title="90th-percentile latency (ms) — one call in ten is at least this slow.">p90 (ms)</th>
        <th title="99th-percentile latency (ms) — the slow tail.">p99 (ms)</th>
        <th title="Estimated USD cost attributed to this purpose (all charges, same basis as Overview's 'Est. total cost').">Est. cost</th>
      </tr>
    </thead>
    <tbody>
      {% for name, p in purpose_rows %}
      <tr>
        <td>{{ name }}</td>
        <td>{{ "{:,}".format(p.calls) }}</td>
        <td>{{ "{:,}".format(p.input) }}</td>
        <td>{{ "{:,}".format(p.output) }}</td>
        <td>{{ ('%.0f' % p.lat_p50) if p.lat_p50 is not none else '—' }}</td>
        <td>{{ ('%.0f' % p.lat_p90) if p.lat_p90 is not none else '—' }}</td>
        <td>{{ ('%.0f' % p.lat_p99) if p.lat_p99 is not none else '—' }}</td>
        <td class="cost good">${{ "%.4f"|format(p.cost) }}</td>
      </tr>
      {% endfor %}
    </tbody>
  </table>

  <h2 title="Why the model decided to stop a given turn. A growing 'max_tokens' share is the usual signal that the output cap needs raising; 'tool_use' counts handoffs to a tool.">Stop reasons</h2>
  {% if not stop_rows %}
    <p class="empty">No stop_reason recorded in this window.</p>
  {% else %}
  <table>
    <thead>
      <tr>
        <th title="Stop reason stamped on the record by the Messages API.">Reason</th>
        <th title="Number of records with this stop reason.">Count</th>
        <th title="Share of records carrying a stop reason that ended this way.">Share</th>
      </tr>
    </thead>
    <tbody>
      {% for name, c in stop_rows %}
      <tr>
        <td>{{ name }}</td>
        <td>{{ "{:,}".format(c) }}</td>
        <td>{{ "%.1f"|format(100.0 * c / stop_total) }}%
          <span class="inline-bar" title="Visual share — {{ '%.1f'|format(100.0 * c / stop_total) }}% of stop reasons in this window."><span class="fill" style="width: {{ (100.0 * c / stop_total)|round(1) }}%"></span></span></td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
  {% endif %}

  <h2 title="API calls bucketed by UTC hour-of-day across the visible window. Tall bars are busy hours. Use this to spot off-hours background traffic, or to size capacity to peak.">Hourly traffic (UTC)</h2>
  {{ chart_hours|safe }}

  <h2 title="Calls bucketed by UTC weekday × hour. Darker cells are heavier. The pattern often picks out workweek office hours, or surfaces background plumbing that runs on a cron.">Weekday × hour heatmap</h2>
  <div class="chart-wrap">{{ chart_heatmap|safe }}</div>

  <h2 title="p50 and p90 latency per UTC day. Diverging lines mean a long tail is opening up — usually a slower model, larger prompts, or contention with a tool call.">Latency over time</h2>
  {{ chart_latency_day|safe }}

  <h2 title="Daily API call counts split by purpose. Watch for background plumbing (title / compaction) growing faster than user-facing turns.">Purpose mix (daily)</h2>
  {{ chart_purpose_stack|safe }}

  {% endif %}"""


# Tools tab — per-tool cost ranking. Each tool record carries the byte size
# of the result it injected back into the prompt; that becomes fresh input on
# the next turn, billed at the emitting turn's model rate (≈ result_bytes / 4
# tokens × model.input). web_search additionally pays a flat per-request
# charge. The donut + table answer "which tool should I trim or gate first?".
_FRAGMENT_TOOLS = """<div class="meta">
    <span title="Filesystem path of the usage.jsonl log being read.">log: {{ log }}</span><br>
    <span title="Records matching the current filters.">{{ record_count }} records</span>
    &middot; <span title="Window the figures cover.">{{ window }}</span>
    {% if user_raw %} &middot; <span>user: {{ user_raw }}</span>{% endif %}
    {% if model_raw %} &middot; <span>model: {{ model_raw }}</span>{% endif %}
</div>

  <div class="cards">
    <div class="card accent-blue"
      title="Total estimated USD cost attributable to tool use in this window. = sum across tool records of (result_bytes / 4 tokens × the turn's model input rate) + exact flat per-request charges for web_search. An estimate, not your invoice — but the figure that moves if you remove a tool.">
      <div class="num blue">${{ '%.2f' % tool_total_cost }}</div>
      <div class="lbl">est. tool cost</div></div>
    <div class="card accent-amber"
      title="Total tool invocations in this window. Counted at the point the result lands (so failed / interrupted tool calls without a returned result are not counted).">
      <div class="num warn">{{ '{:,}'.format(tool_total_calls) }}</div>
      <div class="lbl">tool calls</div></div>
    <div class="card accent-red"
      title="Most expensive tool in this window and its share of estimated tool cost — the first place to look at trimming.">
      <div class="num bad">{{ tool_top_name }}</div>
      <div class="lbl">top tool · ${{ '%.2f' % tool_top_cost }}</div></div>
  </div>

  <h2 title="Daily tool cost stacked by tool name, top 6 by total cost plus an 'other' bucket. The widest band is the tool carrying the most spend in this window.">Cost by tool (daily)</h2>
  {{ chart_tool_stack | safe }}

  <div class="two-col">
    <div>
      <h2 title="Share of estimated tool cost in this window. Slice = (result_bytes / 4 × model input rate) summed across that tool's records, plus its exact web_search flat charge if applicable.">Tool mix</h2>
      {{ chart_tool_donut | safe }}
    </div>
    <div>
      <h2 title="Quick reading guide for the cost figure.">How this is estimated</h2>
      <p>Tool cost is an attribution, not a line item on the invoice — a turn that emits N tool_use blocks is billed as one API call, and each tool_result comes back as fresh input on the next turn.</p>
      <p>For each tool record we estimate <code>result_bytes ÷ 4 ≈ tokens</code>, then price those tokens at the emitting turn's model input rate. <code>web_search</code> adds Anthropic's flat $10 / 1,000 requests on top.</p>
      <p>Removing or gating a tool removes roughly the cost in its row — useful for deciding where to cut.</p>
    </div>
  </div>

  <h2 title="One row per tool, sorted by estimated cost. 'kind' is client (locally executed) or server (Anthropic-side, e.g. web_search). 'result bytes' is the total payload pushed back into prompts — the main cost driver for client tools. 'flat $' is the exact server-tool per-request charge.">By tool</h2>
  <table>
    <thead>
      <tr>
        <th title="Tool name as the agent sees it. Click-through navigation not implemented yet.">Tool</th>
        <th title="Daily estimated-cost trend for this tool across the visible window.">Trend</th>
        <th title="client = executed by Aime's local tool gateway. server = executed by Anthropic (currently only web_search).">Kind</th>
        <th title="Invocations of this tool. Counted at result arrival, so tools whose result never landed (interrupt before tool_result) are not counted.">Calls</th>
        <th title="Total bytes of tool_result this tool injected back into prompts. Approximated as ÷4 to tokens to estimate next-turn input cost.">Result bytes</th>
        <th title="Mean result-payload size for this tool (total bytes ÷ calls). A small, frequent tool can still dominate cost if calls add up.">Avg bytes/call</th>
        <th title="Flat per-request charge ($10 / 1,000) for server-side web_search. Zero for client tools.">Flat $</th>
        <th title="Estimated USD cost = (result_bytes / 4 tokens) × emitting turn's model input rate, plus the flat web_search charge.">Est. cost</th>
        <th title="Share of total estimated tool cost in this window.">Share</th>
      </tr>
    </thead>
    <tbody>
      {% if by_tool %}
        {% for name, t in by_tool %}
        <tr>
          <td>{{ name }}</td>
          <td class="spark">{{ tool_sparklines.get(name, '') | safe }}</td>
          <td class="dim">{{ t.kind }}</td>
          <td>{{ '{:,}'.format(t.calls) }}</td>
          <td>{{ '{:,}'.format(t.result_bytes) }}</td>
          <td>{{ '{:,.0f}'.format((t.result_bytes / t.calls) if t.calls else 0) }}</td>
          <td>{{ ('$%.2f' % (t.web_search_requests * 0.01)) if t.web_search_requests else '—' }}</td>
          <td>${{ '%.4f' % t.cost }}</td>
          <td>{{ ('%.1f%%' % (100.0 * t.cost / tool_total_cost)) if tool_total_cost else '—' }}</td>
        </tr>
        {% endfor %}
      {% else %}
        <tr><td colspan="9" class="dim">No tool records in this window. Tool-use accounting requires <code>AIME_USAGE_STATS=1</code>; the dashboard will start showing rows here once tools fire under that flag.</td></tr>
      {% endif %}
    </tbody>
  </table>
"""


# Agents tab — what each user's headless background-agent runs cost and do.
# Records are tagged source=="agent" by the background-agent runner and keyed
# to the owning user (agent names are unbounded, so we don't break out by
# name); this view excludes interactive chat entirely. Empty-state when no
# agent has run in the window.
_FRAGMENT_AGENTS = """<div class="meta">
    <span title="Filesystem path of the usage.jsonl log being read.">log: {{ log }}</span><br>
    <span title="Records matching the current filters.">{{ record_count }} records</span>
    &middot; <span title="Window the figures cover.">{{ window }}</span>
    {% if user_raw %} &middot; <span>user: {{ user_raw }}</span>{% endif %}
    {% if model_raw %} &middot; <span>model: {{ model_raw }}</span>{% endif %}
</div>

{% if not agents %}
  <p class="dim">No background-agent activity in this window. Headless agent
  runs stamp their usage with <code>source="agent"</code>; rows appear here
  once an agent runs under <code>AIME_USAGE_STATS=1</code>. (Attributing cost
  to a user and counting <em>runs</em> additionally needs
  <code>AIME_USAGE_LINK_USERS=1</code> — otherwise it lands under
  <code>(anonymous)</code>.)</p>
{% else %}

  <div class="cards">
    <div class="card accent-blue"
      title="Real billed USD cost of all background-agent API calls in this window. Same cost basis as the Costs tab (api records only), so it is directly comparable to per-user spend.">
      <div class="num blue">${{ '%.2f' % agent_total_cost }}</div>
      <div class="lbl">agent cost</div></div>
    <div class="card accent-amber"
      title="Share of total estimated spend in this window that was driven by background agents rather than live chat.">
      <div class="num warn">{{ '%.1f%%' % agent_cost_share }}</div>
      <div class="lbl">of total spend</div></div>
    <div class="card accent-green"
      title="Number of background-agent runs in this window — distinct agent sessions across all users. Requires AIME_USAGE_LINK_USERS=1; shows 0 when user/session linkage is off.">
      <div class="num good">{{ '{:,}'.format(agent_total_runs) }}</div>
      <div class="lbl">runs</div></div>
    <div class="card accent-red"
      title="User whose agents cost the most in this window, and that cost — where agent spend concentrates.">
      <div class="num bad">{{ agent_top_name }}</div>
      <div class="lbl">top user · ${{ '%.2f' % agent_top_cost }}</div></div>
  </div>

  <h2 title="Daily background-agent cost stacked by user, top 6 by total cost plus an 'other' bucket. The widest band is the user whose agents carry the most spend in this window.">Agent cost (daily)</h2>
  {{ chart_agent_stack | safe }}

  <div class="two-col">
    <div>
      <h2 title="Share of background-agent cost in this window, by user.">Agent cost by user</h2>
      {{ chart_agent_donut | safe }}
    </div>
    <div>
      <h2 title="How to read this tab.">What this covers</h2>
      <p>Only headless <strong>background-agent</strong> runs appear here — every API call and tool call a run makes is tagged <code>source="agent"</code> and attributed to the user who owns the agent.</p>
      <p>Cost is the real, billed api cost (the same basis as the Costs tab), so a user's agent spend sits on the same scale as their live-chat spend. <em>Tool calls</em> are counted as activity but add no extra cost — their downstream-input cost is already billed on the following turn.</p>
      <p>Use the Model and Purpose filters to see, e.g., only agents' web-search sub-calls or only their main turns.</p>
    </div>
  </div>

  <h2 title="One row per user with background-agent activity, sorted by cost. 'Runs' is distinct sessions (needs user linkage). 'Purpose mix' shows where the cost goes (main turns vs web_search sub-calls vs compaction).">By user</h2>
  <table>
    <thead>
      <tr>
        <th title="User who owns the agents. (anonymous) covers records logged without user linkage. Click through to the user's drill-down.">User</th>
        <th title="Daily agent-cost trend for this user across the visible window.">Trend</th>
        <th title="Distinct agent runs (sessions) for this user. Requires AIME_USAGE_LINK_USERS=1; blank when linkage is off.">Runs</th>
        <th title="Anthropic API calls this user's agents made (main turns + background Haiku calls like web_search/compaction).">API calls</th>
        <th title="Tool invocations this user's agents made (data tools, web_search). Activity only — no extra cost is attributed here.">Tool calls</th>
        <th title="Total input tokens billed across this user's agent API calls.">Input</th>
        <th title="Total output tokens billed across this user's agent API calls.">Output</th>
        <th title="Cache-read tokens served to this user's agents (billed at 0.1x input).">Cache read</th>
        <th title="Server-side web_search requests this user's agents triggered (flat $10 / 1,000).">Web searches</th>
        <th title="Mean real api cost per run = cost ÷ runs. Blank when runs is unknown (linkage off).">$/run</th>
        <th title="Total real api cost attributed to this user's agents.">Est. cost</th>
        <th title="Share of total background-agent cost in this window.">Share</th>
      </tr>
    </thead>
    <tbody>
      {% for name, a in agents %}
      <tr>
        <td title="{{ a.purpose_mix }}"><a href="/user/{{ name }}">{{ name }}</a></td>
        <td class="spark">{{ agent_sparklines.get(name, '') | safe }}</td>
        <td>{{ '{:,}'.format(a.runs) if a.runs else '—' }}</td>
        <td>{{ '{:,}'.format(a.api_calls) }}</td>
        <td>{{ '{:,}'.format(a.tool_calls) }}</td>
        <td>{{ '{:,}'.format(a.input) }}</td>
        <td>{{ '{:,}'.format(a.output) }}</td>
        <td>{{ '{:,}'.format(a.cache_r) }}</td>
        <td>{{ '{:,}'.format(a.web_searches) if a.web_searches else '—' }}</td>
        <td>{{ ('$%.4f' % a.cost_per_run) if a.runs else '—' }}</td>
        <td>${{ '%.4f' % a.cost }}</td>
        <td>{{ ('%.1f%%' % (100.0 * a.cost / agent_total_cost)) if agent_total_cost else '—' }}</td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
{% endif %}
"""


# Model Routing tab — how much the per-turn Haiku/Sonnet router saves vs an
# always-Sonnet baseline, after subtracting the cheap classifier-call
# overhead. Empty-state when no records carry a `routed_decision` (routing
# disabled or pre-routing log entries).
_FRAGMENT_ROUTING = """<div class="meta">
    <span title="Filesystem path of the usage.jsonl log being read.">log: {{ log }}</span><br>
    <span title="Records matching the current filters.">{{ record_count }} records</span>
    &middot; <span title="Window the figures cover.">{{ window }}</span>
    {% if user_raw %} &middot; <span>user: {{ user_raw }}</span>{% endif %}
</div>

{% if routing_total.total_turns == 0 and routing_total.router_calls == 0 %}
  <p class="dim">No routed turns in this window. The Model Routing layer
  records a <code>routed_decision</code> field on each turn it routes; if
  this tab is empty, routing is either disabled
  (<code>AIME_MODEL_ROUTING=0</code>) or the window predates the feature.</p>
{% else %}

  <div class="cards">
    <div class="card accent-green"
      title="Net USD saved by routing in this window. = (Sonnet cost we avoided by routing turns to Haiku) − (cost of the classifier calls themselves). Negative means the router is paying more than it saves — usually a sign the prompt mix is mostly hard turns.">
      <div class="num good">${{ '%.2f' % routing_total.net_savings }}</div>
      <div class="lbl">net savings</div></div>
    <div class="card accent-blue"
      title="Gross USD saved before subtracting classifier overhead. Computed by re-pricing every Haiku-routed turn's token counts at Sonnet's rate and taking the difference.">
      <div class="num blue">${{ '%.2f' % routing_total.haiku_savings }}</div>
      <div class="lbl">gross savings</div></div>
    <div class="card accent-amber"
      title="USD spent on the classifier Haiku calls themselves (purpose=route). Subtracted from gross savings to get net.">
      <div class="num warn">${{ '%.4f' % routing_total.router_cost }}</div>
      <div class="lbl">router overhead</div></div>
    <div class="card accent-blue"
      title="Share of routed turns sent to Haiku. A higher number means the router thinks more of the conversation is read-only lookups.">
      <div class="num">{{ '%.1f%%' % routing_total.haiku_pct }}</div>
      <div class="lbl">{{ '{:,}'.format(routing_total.haiku_turns) }} / {{ '{:,}'.format(routing_total.total_turns) }} → haiku</div></div>
    {% if routing_total.maybe_misclass %}
    <div class="card accent-red"
      title="Haiku-routed turns that look mis-routed: either the model hit max_tokens or the input was Sonnet-sized (>3000 input tokens). Use as a hint to harden the classifier prompt, not a hard error count.">
      <div class="num bad">{{ '{:,}'.format(routing_total.maybe_misclass) }}</div>
      <div class="lbl">likely misclassified</div></div>
    {% endif %}
  </div>

  <h2 title="Per-day count of routed turns split by which model handled them. Watch this for whether the cheap-model share is growing, shrinking, or stable.">Routed turns per day</h2>
  {{ chart_routing_stack | safe }}

  <h2 title="One row per user with at least one routed turn. Sorted by net savings (largest first). 'Likely misclass' counts Haiku-routed turns that look like they should have been Sonnet — use as a tuning hint, not a hard error count.">By user</h2>
  <table>
    <thead>
      <tr>
        <th title="Username, or (anonymous) when user linkage is off.">User</th>
        <th title="Turns the router sent to Haiku.">→ Haiku</th>
        <th title="Turns the router sent to Sonnet.">→ Sonnet</th>
        <th title="Share of this user's routed turns that went to Haiku.">Cheap %</th>
        <th title="USD this user actually spent on Haiku-routed turns.">Haiku $</th>
        <th title="USD Sonnet would have charged for the same token counts. The difference is the gross saving.">Counterfactual $</th>
        <th title="Gross USD saved on Haiku-routed turns (counterfactual − actual).">Gross $</th>
        <th title="USD this user's classifier (route) calls cost.">Router $</th>
        <th title="Net USD saved = gross − router overhead. Negative if the classifier ate more than it saved.">Net $</th>
        <th title="Haiku-routed turns whose stop_reason is max_tokens OR whose input was Sonnet-sized (>3000 input tokens). Hint that the classifier was too generous.">Likely misclass</th>
      </tr>
    </thead>
    <tbody>
      {% for name, u in routing_users %}
      <tr>
        <td><a href="/user/{{ name }}">{{ name }}</a></td>
        <td>{{ '{:,}'.format(u.haiku_turns) }}</td>
        <td>{{ '{:,}'.format(u.sonnet_turns) }}</td>
        <td>{{ '%.1f%%' % u.haiku_pct }}</td>
        <td>${{ '%.4f' % u.haiku_actual }}</td>
        <td>${{ '%.4f' % u.haiku_counterfactual }}</td>
        <td>${{ '%.4f' % u.haiku_savings }}</td>
        <td>${{ '%.4f' % u.router_cost }}</td>
        <td class="{{ 'good' if u.net_savings > 0 else ('bad' if u.net_savings < 0 else 'dim') }}">${{ '%.4f' % u.net_savings }}</td>
        <td class="{{ 'bad' if u.maybe_misclass else 'dim' }}">{{ '{:,}'.format(u.maybe_misclass) if u.maybe_misclass else '—' }}</td>
      </tr>
      {% endfor %}
    </tbody>
  </table>

  <h2 title="How the savings number is computed.">How this is estimated</h2>
  <p>For every turn the router handled, the backend stamps the api record
  with <code>routed_decision</code> (<em>haiku</em> or <em>sonnet</em>). For
  each Haiku-routed turn the dashboard re-prices the exact same token counts
  (input, output, cache reads, cache writes at their TTL splits) at Sonnet's
  base rates; the difference is what routing saved on that turn. Web-search
  flat charges are identical regardless of routing and cancel out.</p>
  <p>The classifier itself is a tiny Haiku call (one user message,
  <code>max_tokens=4</code>) tagged with <code>purpose=route</code>. Its
  actual billed cost is subtracted to get the net figure shown in the
  green card.</p>
{% endif %}

{% if web_search_summary.calls %}
  <h2 title="Savings from running web search on a Haiku sub-agent instead of inline on the conversational model.">Web search offload</h2>
  <div class="cards">
    <div class="card accent-green"
      title="USD saved by offloading web search to Haiku. The sub-agent reads the raw results and returns a digest, so the same token counts are billed at Haiku rates instead of Sonnet's. Computed by re-pricing those counts at Sonnet's rate and taking the difference. Conservative — it ignores the bigger recurring win that raw results never enter the conversation to be re-cached on later turns.">
      <div class="num good">${{ '%.2f' % web_search_summary.savings }}</div>
      <div class="lbl">offload savings</div></div>
    <div class="card accent-blue"
      title="Total searches executed (each billed at the flat $10/1,000 rate) across this many sub-agent calls.">
      <div class="num">{{ '{:,}'.format(web_search_summary.searches) }}</div>
      <div class="lbl">searches / {{ '{:,}'.format(web_search_summary.calls) }} calls</div></div>
    <div class="card accent-amber"
      title="Flat per-search charge ($10 / 1,000). Charged whoever runs the search, so it is NOT part of the savings — shown here as its own cost line.">
      <div class="num warn">${{ '%.2f' % web_search_summary.flat_cost }}</div>
      <div class="lbl">search fees</div></div>
    <div class="card"
      title="Actual USD the web-search sub-agent billed on Haiku (tokens + flat search fees).">
      <div class="num">${{ '%.4f' % web_search_summary.actual }}</div>
      <div class="lbl">haiku cost</div></div>
  </div>
  <p class="dim">Web search runs on a Haiku sub-agent that does the searching,
  reads the raw results, and hands the conversational model a compact digest +
  sources. The bulky raw pages never enter the conversation, so they're never
  re-read from cache on later turns. Savings re-price the sub-agent's token
  counts at Sonnet's rate; the $10/1,000 search fee is identical either way and
  is excluded from savings.</p>
{% endif %}
"""


# Trends tab — period-over-period deltas, anomaly highlights, and a top-N
# table for the single most expensive turns in the window. Designed for a
# weekly-glance "did anything weird happen" read, not deep forensics.
_FRAGMENT_TRENDS = """<div class="meta">
    <span title="Filesystem path of the usage.jsonl log being read.">log: {{ log }}</span><br>
    <span title="Records matching the current filters.">{{ record_count }} records</span>
    &middot;
    <span title="Date range currently in view.">window: {{ window }}</span>
    &middot;
    <span title="The previous window of equal length that the cards compare against.">vs prior {{ prev_window }}</span>
  </div>

  {% for e in errors %}<p class="err">{{ e }}</p>{% endfor %}

  <div class="cards">
    <div class="card accent-green"
      title="Total estimated USD cost in the current window vs the previous same-length window. A red 'up' arrow flags growing spend; green 'down' flags savings.">
      <div class="num good">${{ "%.4f"|format(curr.cost) }}</div>
      <div class="lbl">cost</div>
      {% if delta.cost is none %}
        <div class="delta flat">no prior baseline</div>
      {% else %}
        <div class="delta {{ 'up' if delta.cost > 0 else 'down' if delta.cost < 0 else 'flat' }}">
          {{ "%+.1f"|format(delta.cost) }}% vs prior
          (${{ "%.4f"|format(prev.cost) }})</div>
      {% endif %}
    </div>
    <div class="card accent-blue"
      title="API call count, current vs prior period.">
      <div class="num blue">{{ "{:,}".format(curr.calls) }}</div>
      <div class="lbl">API calls</div>
      {% if delta.calls is none %}
        <div class="delta flat">no prior baseline</div>
      {% else %}
        <div class="delta {{ 'up' if delta.calls > 0 else 'down' if delta.calls < 0 else 'flat' }}">
          {{ "%+.1f"|format(delta.calls) }}% vs prior
          ({{ "{:,}".format(prev.calls) }})</div>
      {% endif %}
    </div>
    <div class="card accent-purple"
      title="Fresh-input + output tokens, current vs prior period.">
      <div class="num purple">{{ "{:,}".format(curr.tokens) }}</div>
      <div class="lbl">tokens</div>
      {% if delta.tokens is none %}
        <div class="delta flat">no prior baseline</div>
      {% else %}
        <div class="delta {{ 'up' if delta.tokens > 0 else 'down' if delta.tokens < 0 else 'flat' }}">
          {{ "%+.1f"|format(delta.tokens) }}% vs prior
          ({{ "{:,}".format(prev.tokens) }})</div>
      {% endif %}
    </div>
    <div class="card accent-amber"
      title="Distinct active users in the current vs prior period. Up is usually good (more engagement); down often precedes a cost dip.">
      <div class="num warn">{{ curr.users }}</div>
      <div class="lbl">active users</div>
      {% if delta.users is none %}
        <div class="delta flat">no prior baseline</div>
      {% else %}
        <div class="delta {{ 'up' if delta.users > 0 else 'down' if delta.users < 0 else 'flat' }}">
          {{ "%+.1f"|format(delta.users) }}% vs prior
          ({{ prev.users }})</div>
      {% endif %}
    </div>
  </div>

  <h2 title="Daily total cost trend across the visible window.">Daily cost</h2>
  {{ chart_daily_cost|safe }}

  <h2 title="Active users per UTC day — distinct usernames that logged at least one API call.">Active users per day</h2>
  {{ chart_active_users|safe }}

  {% if anomalies %}
  <h2 title="Days whose cost exceeded the window mean by more than 2 standard deviations. A naive outlier flag — useful for spotting runaway loops or one-off heavy turns.">Anomaly days</h2>
  <table>
    <thead>
      <tr>
        <th title="UTC date.">Date</th>
        <th title="Total cost on that day.">Cost</th>
        <th title="Standard deviations above the window mean.">z-score</th>
      </tr>
    </thead>
    <tbody>
      {% for d, c, z in anomalies %}
      <tr>
        <td>{{ d }}</td>
        <td class="cost bad">${{ "%.4f"|format(c) }}</td>
        <td class="bad">{{ "%.2f"|format(z) }}&sigma;</td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
  {% endif %}

  <h2 title="The single most expensive API calls in this window. Each row is one record from usage.jsonl.">Most expensive turns</h2>
  {% if not top_calls %}
    <p class="empty">No API records in this window.</p>
  {% else %}
  <table>
    <thead>
      <tr>
        <th title="UTC timestamp of the request.">When</th>
        <th title="User the request was logged under.">User</th>
        <th title="Model that served the request.">Model</th>
        <th title="Purpose tag stamped on the record.">Purpose</th>
        <th title="Fresh input tokens.">Input</th>
        <th title="Output tokens.">Output</th>
        <th title="Wall-clock latency.">Latency</th>
        <th title="Estimated USD cost for this single call.">Cost</th>
      </tr>
    </thead>
    <tbody>
      {% for cost, rec in top_calls %}
      <tr>
        <td>{{ rec.ts|truncate(19, true, '') }}</td>
        <td><a href="/user/{{ (rec.user or '(anonymous)')|urlencode }}" class="userlink">{{ rec.user or '(anonymous)' }}</a></td>
        <td>{{ rec.model or '(unknown)' }}</td>
        <td>{{ rec.purpose or '(unspecified)' }}</td>
        <td>{{ "{:,}".format(rec.input_tokens or 0) }}</td>
        <td>{{ "{:,}".format(rec.output_tokens or 0) }}</td>
        <td>{{ ("%.0f ms" % rec.duration_ms) if rec.duration_ms is not none else '—' }}</td>
        <td class="cost bad">${{ "%.4f"|format(cost) }}</td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
  {% endif %}

  <h2 title="Cost share by user across the window — heaviest first.">Top users by spend</h2>
  {% if not top_users %}
    <p class="empty">No API records in this window.</p>
  {% else %}
  <table>
    <thead>
      <tr>
        <th>User</th>
        <th>Calls</th>
        <th>Cost</th>
        <th title="Share of total window cost.">Share</th>
        <th title="Per-day trend.">Trend</th>
      </tr>
    </thead>
    <tbody>
      {% for name, u in top_users %}
      <tr>
        <td><a href="/user/{{ name|urlencode }}" class="userlink">{{ name }}</a></td>
        <td>{{ "{:,}".format(u.api_calls) }}</td>
        <td class="cost good">${{ "%.4f"|format(u.cost) }}</td>
        <td>{{ "%.1f"|format(100.0 * u.cost / grand_cost if grand_cost else 0) }}%</td>
        <td class="spark">{{ user_sparklines.get(name, '')|safe }}</td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
  {% endif %}"""


# Users tab — engagement view: simplified status (new / active / dormant)
# plus an in-browser pattern chip filter that hides rows by log frequency.
_FRAGMENT_USERS = """<div class="meta">
    <span title="Filesystem path of the usage.jsonl log being read.">log: {{ log }}</span><br>
    <span title="Records matching the current filters.">{{ record_count }} records</span>
    &middot;
    <span title="Date range currently in view.">window: {{ window }}</span>
    &middot;
    <span title="Cohort window used by the New / Returning / Dormant lists.">cohort: {{ cohort_window }}</span>
  </div>

  {% for e in errors %}<p class="err">{{ e }}</p>{% endfor %}

  <div class="cards">
    <div class="card accent-blue"
      title="Distinct users with at least one API record in this window.">
      <div class="num blue">{{ counts.active }}</div>
      <div class="lbl">active in window</div></div>
    <div class="card accent-green"
      title="Users whose first-ever record falls within this window — fresh signups, effectively.">
      <div class="num good">{{ counts.new }}</div>
      <div class="lbl">new this window</div></div>
    <div class="card accent-amber"
      title="Users whose latest record is more than {{ dormant_days }} days old AND are not active in this window. Candidates for a re-engagement nudge.">
      <div class="num warn">{{ counts.dormant }}</div>
      <div class="lbl">dormant ({{ dormant_days }}d+)</div></div>
    <div class="card accent-purple"
      title="Users averaging 5 or more API calls per active day in this window — the 'logged on multiple times per day' cohort.">
      <div class="num purple">{{ counts.heavy }}</div>
      <div class="lbl">heavy (≥5 calls / active day)</div></div>
  </div>

  <h2 title="Distinct active users per UTC day in the visible window. A steady line means a healthy returning base; a saw-tooth means traffic is bursty.">Daily active users</h2>
  {{ chart_active_users|safe }}

  {% if classification.new %}
  <h2 title="Users whose first record is inside the current window. Sorted alphabetically.">New this window ({{ classification.new|length }})</h2>
  <p class="userchips">
    {% for n in classification.new %}<a class="chip" href="/user/{{ n|urlencode }}">{{ n }}</a>{% endfor %}
  </p>
  {% endif %}

  {% if classification.dormant %}
  <h2 title="Users with no record in the last {{ dormant_days }} days. Ordered by 'most dormant first' — the top of the list is the freshest re-engagement candidate.">Dormant users ({{ classification.dormant|length }})</h2>
  <p class="userchips">
    {% for n in classification.dormant %}<a class="chip" href="/user/{{ n|urlencode }}">{{ n }}</a>{% endfor %}
  </p>
  {% endif %}

  <h2 title="One row per user that has ever appeared in the log, ordered by lifetime cost.">All users</h2>
  {% if not classification.rows %}
    <p class="empty">No users have any logged API records yet.</p>
  {% else %}

  <div class="userfilter" title="Filter the table below by engagement pattern. The filter runs in the browser — no reload — so it composes with the date / model / purpose filter above without losing your scroll position.">
    <span class="lbl">Pattern:</span>
    <button type="button" class="chipbtn active" data-pattern="all"
      title="Show every user, regardless of how often they appeared in the window.">All <span class="note">({{ classification.rows|length }})</span></button>
    <button type="button" class="chipbtn" data-pattern="daily"
      title="Users that appeared on every UTC day of the current window — the most consistent cohort.">Every day <span class="note">({{ pattern_counts['daily'] }})</span></button>
    <button type="button" class="chipbtn" data-pattern="most-days"
      title="Users active on at least half of the window's days, but not every day.">Most days <span class="note">({{ pattern_counts['most-days'] }})</span></button>
    <button type="button" class="chipbtn" data-pattern="occasional"
      title="Users active on two or more days, below the 'most days' threshold.">Occasional <span class="note">({{ pattern_counts['occasional'] }})</span></button>
    <button type="button" class="chipbtn" data-pattern="once"
      title="Users with activity on exactly one day in the window — a single visit, possibly a try-out.">Once <span class="note">({{ pattern_counts['once'] }})</span></button>
    <button type="button" class="chipbtn" data-pattern="heavy"
      title="Users averaging 5 or more API calls per active day — the 'logged on multiple times per day' cohort.">Multiple/day <span class="note">({{ counts.heavy }})</span></button>
    <button type="button" class="chipbtn" data-pattern="dormant"
      title="Users with no record in the last {{ dormant_days }} days.">Dormant <span class="note">({{ counts.dormant }})</span></button>
  </div>

  <table id="userstable">
    <thead>
      <tr>
        <th>User</th>
        <th title="Classification in the current window: new / active / dormant.">Status</th>
        <th title="Engagement pattern in the window: daily / most-days / occasional / once / none.">Pattern</th>
        <th title="Distinct UTC days in the window the user appeared on.">Active days</th>
        <th title="Mean API calls per active day in the window. A '!' flag marks users averaging 5+ — the 'multiple per day' cohort.">Calls/day</th>
        <th title="UTC timestamp of the very first record for this user.">First seen</th>
        <th title="UTC timestamp of the most recent record for this user.">Last seen</th>
        <th title="Days since the last record (relative to the window end).">Idle</th>
        <th title="Lifetime API call count.">Lifetime calls</th>
        <th title="Lifetime estimated USD cost.">Lifetime cost</th>
        <th title="API calls in the current window.">Window calls</th>
        <th title="Estimated USD cost in the current window.">Window cost</th>
      </tr>
    </thead>
    <tbody>
      {% for r in classification.rows %}
      <tr data-status="{{ r.status }}" data-pattern="{{ r.pattern }}" data-heavy="{{ '1' if r.heavy else '0' }}">
        <td><a href="/user/{{ r.username|urlencode }}" class="userlink">{{ r.username }}</a></td>
        <td><span class="status-pill status-{{ r.status }}">{{ r.status }}</span></td>
        <td><span class="pattern-pill pattern-{{ r.pattern }}">{{ r.pattern }}</span></td>
        <td>{{ r.active_days }} <span class="note">/ {{ classification.window_days }}</span></td>
        <td class="{{ 'warn' if r.heavy else '' }}">{{ "%.1f"|format(r.calls_per_active_day) }}{% if r.heavy %} <span title="averages 5+ calls per active day">!</span>{% endif %}</td>
        <td>{{ r.first_seen|truncate(19, true, '') }}</td>
        <td>{{ r.last_seen|truncate(19, true, '') }}</td>
        <td class="{{ 'bad' if r.days_since_last >= dormant_days else 'warn' if r.days_since_last >= 7 else '' }}">{{ r.days_since_last }}d</td>
        <td>{{ "{:,}".format(r.lifetime_calls) }}</td>
        <td class="cost good">${{ "%.4f"|format(r.lifetime_cost) }}</td>
        <td>{{ "{:,}".format(r.window_calls) }}</td>
        <td class="cost good">${{ "%.4f"|format(r.window_cost) }}</td>
      </tr>
      {% endfor %}
    </tbody>
  </table>

  {% endif %}"""


# Conversations tab — one row per session_id, sorted by cost. The biggest gap
# the old dashboard had: even with session_id recorded on every record, there
# was no place to ask "which conversations are running up the bill?" or
# "what's the typical conversation shape?". Click into /session/<id> for the
# detailed timeline.
_FRAGMENT_CONVERSATIONS = """<div class="meta">
    <span title="Filesystem path of the usage.jsonl log being read.">log: {{ log }}</span><br>
    <span title="Records matching the current filters.">{{ record_count }} records</span>
    &middot; <span title="Window the figures cover.">{{ window }}</span>
  </div>

  {% for e in errors %}<p class="err">{{ e }}</p>{% endfor %}

  {% if anonymized %}
  <div class="banner warn">
    <strong>User attribution is disabled.</strong>
    Conversation grouping needs a session id on each record, which is gated
    behind <code>AIME_USAGE_LINK_USERS=1</code>. Set it to populate this view.
  </div>
  {% endif %}

  {% if not sessions %}
    <p class="empty">No conversations have been recorded in this window.</p>
  {% else %}

  <div class="cards">
    <div class="card accent-blue"
      title="Distinct conversations in this window — every record carrying a session_id is one conversation.">
      <div class="num blue">{{ '{:,}'.format(session_count) }}</div>
      <div class="lbl">conversations</div></div>
    <div class="card accent-green"
      title="Total billed cost across every conversation in this window.">
      <div class="num good">${{ '%.2f'|format(sessions_total_cost) }}</div>
      <div class="lbl">$ across conversations</div></div>
    <div class="card accent-purple"
      title="Mean billed cost per conversation. A small handful of expensive sessions usually dominate the average — see the table below.">
      <div class="num purple">${{ '%.4f'|format(sessions_avg_cost) }}</div>
      <div class="lbl">avg $ / conversation</div></div>
    <div class="card accent-amber"
      title="Mean assistant turns per conversation (purpose=turn api records grouped by session).">
      <div class="num warn">{{ '%.1f'|format(sessions_avg_turns) }}</div>
      <div class="lbl">avg turns / conversation</div></div>
  </div>

  <h2 title="One row per conversation, heaviest first. Click 'open' for the per-session timeline.">Conversations</h2>
  <p class="note">
    A <em>turn</em> is one assistant reply (purpose=turn). <em>Tool calls</em>
    counts tool_result records inside the conversation. <em>Top tool</em> is
    the tool invoked most often. The compaction icon (&#8634;) appears when
    a conversation has triggered Haiku-based compaction at least once.
  </p>
  <table>
    <thead>
      <tr>
        <th title="Conversation owner. Click the username to open their drill-down.">User</th>
        <th title="Session id — the on-disk name of the encrypted conversation file. Truncated for display; the full id is in the link target.">Session</th>
        <th title="Local timestamp of the first record in this conversation.">Started</th>
        <th title="Local timestamp of the most recent record.">Last active</th>
        <th title="Length of the conversation in minutes (first to last record).">Duration</th>
        <th title="Number of assistant turns (purpose=turn API records).">Turns</th>
        <th title="Tool invocations made during this conversation.">Tools</th>
        <th title="Model that carried the most $ within this conversation.">Top model</th>
        <th title="Tool invoked most often.">Top tool</th>
        <th title="Flags. ⟲ = compaction ran. ⚠ = at least one turn hit max_tokens.">Flags</th>
        <th title="Total billed cost of every record in this conversation, including any compaction overhead.">$ cost</th>
        <th title="Open the per-conversation timeline.">&nbsp;</th>
      </tr>
    </thead>
    <tbody>
      {% for s in sessions %}
      <tr>
        <td><a href="/user/{{ s.user|urlencode }}" class="userlink">{{ s.user }}</a></td>
        <td><code title="{{ s.session_id }}">{{ s.session_id[:10] }}…</code></td>
        <td>{{ s.first_ts[:16] }}</td>
        <td>{{ s.last_ts[:16] }}</td>
        <td>{{ '%.0f'|format(s.duration_min) }}m</td>
        <td>{{ s.turns }}</td>
        <td>{{ s.tool_calls }}</td>
        <td>{{ s.top_model }}</td>
        <td>{{ s.top_tool }}</td>
        <td>
          {%- if s.compaction_calls -%}<span title="Compaction ran {{ s.compaction_calls }}× — Haiku folded older messages into a summary.">⟲</span>{%- endif -%}
          {%- if s.max_tokens_hits -%} <span class="warn" title="{{ s.max_tokens_hits }} turn(s) hit max_tokens — output was truncated.">⚠</span>{%- endif -%}
          {%- if not s.compaction_calls and not s.max_tokens_hits -%}—{%- endif -%}
        </td>
        <td class="cost good">${{ '%.4f'|format(s.cost) }}</td>
        <td><a href="/session/{{ s.session_id|urlencode }}" class="userlink">open →</a></td>
      </tr>
      {% endfor %}
    </tbody>
    <tfoot>
      <tr>
        <td colspan="10">Total ({{ session_count }} conversation{{ '' if session_count == 1 else 's' }})</td>
        <td class="cost good">${{ '%.4f'|format(sessions_total_cost) }}</td>
        <td></td>
      </tr>
    </tfoot>
  </table>

  {% endif %}"""


# Per-session detail — the timeline of records belonging to one session_id,
# with breakdowns by model / tool / purpose. Reached from the Conversations
# tab and from a user's drill-down.
_FRAGMENT_SESSION = """<div class="meta">
    <span title="Conversation owner.">user: <strong><a href="/user/{{ header.user|urlencode }}" class="userlink">{{ header.user }}</a></strong></span><br>
    <span title="On-disk session id of the encrypted conversation file.">session: <code>{{ header.session_id }}</code></span>
    &middot; <span title="Number of records in this conversation.">{{ header.record_count }} records</span>
  </div>

  <p><a href="/?tab=conversations">&larr; back to conversations</a></p>

  {% if not timeline %}
    <p class="empty">No records found for this session.</p>
  {% else %}

  <div class="cards">
    <div class="card accent-green"
      title="Total billed cost for this conversation: every API + tool record summed.">
      <div class="num good">${{ '%.4f'|format(total_cost) }}</div>
      <div class="lbl">$ for this conversation</div></div>
    <div class="card accent-blue"
      title="Assistant turns (purpose=turn API records). Excludes background plumbing (title, compaction) and tool records.">
      <div class="num blue">{{ header.turns }}</div>
      <div class="lbl">turns</div></div>
    <div class="card accent-purple"
      title="Tool invocations during this conversation.">
      <div class="num purple">{{ header.tool_calls }}</div>
      <div class="lbl">tool calls</div></div>
    <div class="card accent-amber"
      title="Background Haiku compaction calls — fired by the backend when history grew past the compaction threshold.">
      <div class="num warn">{{ header.compaction_calls }}</div>
      <div class="lbl">compactions</div></div>
    {% if header.max_tokens_hits %}
    <div class="card accent-red"
      title="Turns that ended in stop_reason=max_tokens — the model output was truncated.">
      <div class="num bad">{{ header.max_tokens_hits }}</div>
      <div class="lbl">max_tokens hits</div></div>
    {% endif %}
  </div>

  <div class="two-col">
    <div>
      <h2 title="Share of cost by model within this conversation.">By model</h2>
      {{ chart_model_donut|safe }}
    </div>
    <div>
      <h2 title="Share of cost by purpose — user turns vs background plumbing.">By purpose</h2>
      {{ chart_purpose_donut|safe }}
    </div>
  </div>

  {% if by_tool %}
  <h2 title="Tool invocations grouped by name, ordered by attributed cost.">Tools used</h2>
  <table>
    <thead>
      <tr>
        <th>Tool</th>
        <th>Calls</th>
        <th>$ attributed</th>
      </tr>
    </thead>
    <tbody>
      {% for name, cost in by_tool %}
      <tr>
        <td>{{ name }}</td>
        <td>{{ tool_call_counts.get(name, 0) }}</td>
        <td class="cost good">${{ '%.4f'|format(cost) }}</td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
  {% endif %}

  <h2 title="Every record in this conversation, oldest first. Tool calls inline with the API turns that produced them.">Timeline</h2>
  <table>
    <thead>
      <tr>
        <th>When</th>
        <th>Kind</th>
        <th>Purpose / tool</th>
        <th>Model / route</th>
        <th>Tokens in/out</th>
        <th>Stop</th>
        <th>Latency</th>
        <th>$</th>
      </tr>
    </thead>
    <tbody>
      {% for r in timeline %}
      <tr class="{{ 'dim' if r.kind == 'tool' else '' }}">
        <td>{{ r.ts[:19] }}</td>
        <td>{{ r.kind }}</td>
        <td>
          {%- if r.kind == 'api' -%}{{ r.purpose }}
          {%- else -%}{{ r.tool_name }} ({{ '{:,}'.format(r.result_bytes) }} B)
          {%- endif -%}
        </td>
        <td>{{ r.model }}{% if r.routed_decision %} <span class="note">→{{ r.routed_decision }}</span>{% endif %}</td>
        <td>{% if r.kind == 'api' %}{{ '{:,}'.format(r.tokens_in) }} / {{ '{:,}'.format(r.tokens_out) }}{% else %}—{% endif %}</td>
        <td class="{{ 'bad' if r.stop_reason == 'max_tokens' else '' }}">{{ r.stop_reason or '—' }}</td>
        <td>{{ ('%.0f ms' % r.duration_ms) if r.duration_ms is not none else '—' }}</td>
        <td class="cost good">${{ '%.5f'|format(r.cost) }}</td>
      </tr>
      {% endfor %}
    </tbody>
  </table>

  {% endif %}"""


# Per-user drill-down — KPI cards, daily cost line, model & purpose donuts,
# recent activity table. Linked from the Overview "By user" table.
_FRAGMENT_USER = """<div class="meta">
    <span title="Username being inspected.">user: <strong>{{ username }}</strong></span>
    {% if behavior.badge %}<span class="badge badge-{{ behavior.badge }}" title="Activity profile derived from how often this user shows up and how heavy their turns are. power = ≥14 active days and ≥6 messages/session; regular = ≥7 active days; casual = 2–6 active days; one-off = single active day; none = no traffic.">{{ behavior.badge }}</span>{% endif %}
    <br>
    <span title="All-time figures across this user's entire history.">{{ record_count }} records</span>
  </div>

  <p><a href="/?tab=users">&larr; back to users</a></p>

  {% if not has_data %}
    <p class="empty">No API records found for this user.</p>
  {% else %}

  <div class="cards">
    <div class="card accent-green"
      title="Lifetime billed cost for this user — every charge stamped on every API and tool record.">
      <div class="num good">${{ "%.4f"|format(u.cost) }}</div>
      <div class="lbl">$ spent (lifetime)</div></div>
    <div class="card accent-blue"
      title="Distinct conversations (session_ids) this user has had. Requires AIME_USAGE_LINK_USERS=1; '—' otherwise.">
      <div class="num blue">{{ behavior.sessions if behavior.sessions else '—' }}</div>
      <div class="lbl">conversations</div></div>
    <div class="card accent-purple"
      title="Mean assistant turns per conversation — a quick read on whether this user's conversations are short Q&A or deep multi-turn work.">
      <div class="num purple">{{ '%.1f'|format(behavior.msgs_per_session) }}</div>
      <div class="lbl">avg turns / conversation</div></div>
    <div class="card accent-amber"
      title="Distinct calendar days with at least one API record. Counts engagement breadth — daily users vs binge-and-vanish users.">
      <div class="num warn">{{ behavior.active_days }}</div>
      <div class="lbl">active days (lifetime)</div></div>
    <div class="card accent-blue"
      title="Median billed cost of an assistant turn. Low = short Q&A user; high = heavy reasoning / large prompts.">
      <div class="num blue">${{ '%.5f'|format(behavior.median_turn_cost) }}</div>
      <div class="lbl">median $ / turn</div></div>
    <div class="card {{ 'accent-green' if cache_hit_pct >= 70 else 'accent-amber' if cache_hit_pct >= 40 else 'accent-red' }}"
      title="Share of read-side prompt tokens served from cache (cheap) rather than billed fresh. Higher is cheaper. Green ≥70%, amber 40–70%, red <40%.">
      <div class="num {{ 'good' if cache_hit_pct >= 70 else 'warn' if cache_hit_pct >= 40 else 'bad' }}">{{ "%.0f"|format(cache_hit_pct) }}%</div>
      <div class="lbl">cache hit rate</div></div>
  </div>

  <h2 title="Cost charged to this user per day across their full history.">Cost over time</h2>
  {{ chart_daily_cost|safe }}

  <div class="two-col">
    <div>
      <h2 title="Share of this user's spend by model.">Model mix</h2>
      {{ chart_model_donut|safe }}
    </div>
    <div>
      <h2 title="Share of this user's spend by call purpose. 'turn' is a user-facing reply; 'title' / 'compaction' are background Haiku jobs the user never sees directly.">Purpose mix</h2>
      {{ chart_purpose_donut|safe }}
    </div>
  </div>

  <div class="two-col">
    <div>
      <h2 title="Calls bucketed by weekday × hour. Darker cells are heavier — a quick visual read of when this user actually talks to Aime.">When they talk</h2>
      <div class="chart-wrap">{{ chart_user_heatmap|safe }}</div>
    </div>
    <div>
      <h2 title="Tools this user invokes most often. Strong signal for what they actually use Aime for.">Top tools</h2>
      {% if behavior.top_tools %}
      <table>
        <thead><tr><th>Tool</th><th>Calls</th></tr></thead>
        <tbody>
          {% for name, count in behavior.top_tools %}
          <tr><td>{{ name }}</td><td>{{ '{:,}'.format(count) }}</td></tr>
          {% endfor %}
        </tbody>
      </table>
      {% else %}
        <p class="empty">No tool calls recorded for this user yet.</p>
      {% endif %}
      {% if behavior.routing_rate_pct is not none %}
      <p class="note" title="Share of this user's routed turns that the Haiku/Sonnet classifier sent to Haiku. Higher = more of their turns are lookup-shaped.">
        Router rate: <strong>{{ '%.0f%%'|format(behavior.routing_rate_pct) }}</strong> of turns go to Haiku.
      </p>
      {% endif %}
    </div>
  </div>

  <h2 title="Every conversation this user has had, newest first. Click 'open' for the timeline.">Conversations ({{ user_sessions|length }})</h2>
  {% if not user_sessions %}
    <p class="empty">No grouped conversations — session_id is gated behind AIME_USAGE_LINK_USERS=1.</p>
  {% else %}
  <table>
    <thead>
      <tr>
        <th>Started</th>
        <th>Turns</th>
        <th>Tools</th>
        <th>Top model</th>
        <th>Top tool</th>
        <th>Duration</th>
        <th>$ cost</th>
        <th>&nbsp;</th>
      </tr>
    </thead>
    <tbody>
      {% for s in user_sessions %}
      <tr>
        <td>{{ s.first_ts[:16] }}</td>
        <td>{{ s.turns }}</td>
        <td>{{ s.tool_calls }}</td>
        <td>{{ s.top_model }}</td>
        <td>{{ s.top_tool }}</td>
        <td>{{ '%.0f'|format(s.duration_min) }}m</td>
        <td class="cost good">${{ '%.4f'|format(s.cost) }}</td>
        <td><a href="/session/{{ s.session_id|urlencode }}" class="userlink">open →</a></td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
  {% endif %}

  {% endif %}"""


# System tab — operator-level health. Counts and sizes only; never reads
# topic, event, or conversation content. The user's data stays opaque.
_FRAGMENT_SYSTEM = """
  <div class="cards">
    <div class="card accent-blue"
      title="Active accounts in this deployment.">
      <div class="num blue">{{ n_active }}</div>
      <div class="lbl">active accounts</div></div>
    <div class="card accent-green"
      title="Active accounts whose api_access flag is on — they may send messages through the paid model backend.">
      <div class="num good">{{ n_with_send_access }}</div>
      <div class="lbl">with send access</div></div>
    <div class="card accent-amber"
      title="Soft-deleted accounts still within their grace period. Data is retained until purge.">
      <div class="num warn">{{ n_deleted }}</div>
      <div class="lbl">soft-deleted</div></div>
    <div class="card accent-purple"
      title="Total disk used by every per-user data directory plus shared state under AIME_DATABASE_DIR. Includes the usage log, auth.sql, backups, and per-user databases and conversations.">
      <div class="num purple">{{ db_dir_size_h }}</div>
      <div class="lbl">database dir size</div></div>
  </div>

  <div class="cards">
    <div class="card accent-blue"
      title="Invite keys minted, redeemed or not.">
      <div class="num blue">{{ n_keys_total }}</div>
      <div class="lbl">invite keys total</div></div>
    <div class="card accent-green"
      title="Invite keys that have been redeemed and turned into an active account.">
      <div class="num good">{{ n_keys_redeemed }}</div>
      <div class="lbl">keys redeemed</div></div>
    <div class="card accent-amber"
      title="Invite keys still minted but unused — eligible to be revoked or redeemed.">
      <div class="num warn">{{ n_keys_unredeemed }}</div>
      <div class="lbl">keys unredeemed</div></div>
    <div class="card accent-purple"
      title="Size on disk of the append-only usage.jsonl log (drives every figure on the Overview / Cache / Activity tabs).">
      <div class="num purple">{{ log_size_h }}</div>
      <div class="lbl">usage log size</div></div>
  </div>

  <p class="note">Database root: <code>{{ db_dir }}</code></p>

  <h2 title="Per-user storage occupancy. Sizes and file counts only — topic, event, and conversation contents are never read by this dashboard.">Storage per user</h2>
  {% if not per_user %}
    <p class="empty">No active per-user data directories.</p>
  {% else %}
  <table>
    <thead>
      <tr>
        <th title="Internal account id (matches the on-disk users/&lt;id&gt;/ directory).">ID</th>
        <th title="Account username.">Username</th>
        <th title="Total bytes under the user's data directory (database.sql, topics/, conversations/, etc.). Recursive sum of file sizes — files are never opened.">Size</th>
        <th title="Number of .md files in users/&lt;id&gt;/topics/. The dashboard counts entries only; topic contents are not read.">Topics</th>
        <th title="Number of .json files in users/&lt;id&gt;/conversations/. These are encrypted on disk; the dashboard counts entries only.">Conversations</th>
        <th title="Whether the per-user data directory currently exists on disk. A 'no' usually means the user has signed up but never created any data.">Dir exists</th>
      </tr>
    </thead>
    <tbody>
      {% for u in per_user %}
      <tr>
        <td>#{{ u.id }}</td>
        <td>{{ u.username }}</td>
        <td>{{ u.size_h }}</td>
        <td>{{ "{:,}".format(u.topics) }}</td>
        <td>{{ "{:,}".format(u.conversations) }}</td>
        <td class="{{ 'good' if u.exists else 'warn' }}">{{ 'yes' if u.exists else 'no' }}</td>
      </tr>
      {% endfor %}
    </tbody>
    <tfoot>
      <tr>
        <td colspan="2">Total</td>
        <td>{{ per_user_size_h }}</td>
        <td>{{ "{:,}".format(per_user_topics_total) }}</td>
        <td>{{ "{:,}".format(per_user_conversations_total) }}</td>
        <td></td>
      </tr>
    </tfoot>
  </table>
  <p class="note">Sizes and counts are read directly from the filesystem.
    No topic, event, or conversation content is ever opened by this view.</p>
  {% endif %}"""


# Security tab. Surfaces the auth_events audit log so the operator can spot
# abnormal login/signup activity without having to grep auth.sql by hand.
_FRAGMENT_SECURITY = """
  <div class="cards">
    <div class="card accent-red"
      title="Failed logins where the username does not match any account. A spike here usually means someone is iterating through guessed usernames.">
      <div class="num bad">{{ counts_24h.login_unknown_user }}</div>
      <div class="lbl">unknown-user logins (24h)</div></div>
    <div class="card accent-amber"
      title="Failed logins against a real account with a wrong password. Per-account lockout kicks in after 5 of these in a 15-minute window.">
      <div class="num warn">{{ counts_24h.login_bad_password }}</div>
      <div class="lbl">bad password (24h)</div></div>
    <div class="card accent-red"
      title="Login attempts against an account that is currently locked out. Each one means the lockout is doing its job.">
      <div class="num bad">{{ counts_24h.login_while_locked }}</div>
      <div class="lbl">attempts on locked accounts (24h)</div></div>
    <div class="card accent-amber"
      title="Accounts that crossed the failure threshold and got locked during this window.">
      <div class="num warn">{{ counts_24h.lockout_started }}</div>
      <div class="lbl">lockouts started (24h)</div></div>
    <div class="card accent-red"
      title="Login attempts rejected by the per-IP rate limiter after too many failures from one source IP. A spike means a single host is spraying passwords across accounts.">
      <div class="num bad">{{ counts_24h.login_ip_throttled }}</div>
      <div class="lbl">IP-throttled logins (24h)</div></div>
  </div>

  <div class="cards">
    <div class="card accent-red"
      title="Signup attempts blocked by the per-IP rate limiter (5 per hour per source IP). A spike means someone is trying to bulk-register accounts.">
      <div class="num bad">{{ counts_24h.signup_rate_limited }}</div>
      <div class="lbl">signup rate-limited (24h)</div></div>
    <div class="card accent-amber"
      title="Signup form submissions that failed validation (weak password, invalid username, taken username, invalid email).">
      <div class="num warn">{{ counts_24h.signup_failed }}</div>
      <div class="lbl">signup failures (24h)</div></div>
    <div class="card accent-blue"
      title="Same counters, narrowed to the last hour. A 1h:24h ratio close to 1.0 means activity is concentrated right now.">
      <div class="num blue">{{ total_1h }}</div>
      <div class="lbl">total events (1h)</div></div>
    <div class="card accent-purple"
      title="Retention window for the audit log. Older rows are pruned opportunistically on every write.">
      <div class="num purple">{{ retention_days }}d</div>
      <div class="lbl">audit log retention</div></div>
  </div>

  <h2 title="Source IPs with the highest auth-failure count in the last 24 hours. NULL IPs (background callers) are excluded. If one IP dominates, consider blocking it upstream in Caddy.">Top source IPs (24h)</h2>
  {% if not top_ips %}
    <p class="empty">No IP-attributed auth failures in the last 24 hours.</p>
  {% else %}
  <table>
    <thead>
      <tr>
        <th title="Source IP (post-ProxyFix, i.e. the real client IP behind the reverse proxy).">IP</th>
        <th title="Number of auth-failure events from this IP in the window.">Events</th>
        <th title="Most recent failure timestamp from this IP, in the dashboard time zone.">Last seen</th>
      </tr>
    </thead>
    <tbody>
      {% for r in top_ips %}
      <tr>
        <td><code>{{ r.ip }}</code></td>
        <td>{{ "{:,}".format(r.count) }}</td>
        <td>{{ r.last_ts_h }}</td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
  {% endif %}

  <h2 title="The newest {{ events|length }} auth events, regardless of kind. Most recent first.">Recent events</h2>
  {% if not events %}
    <p class="empty">No auth events recorded yet.</p>
  {% else %}
  <table>
    <thead>
      <tr>
        <th title="When the event was recorded, in the dashboard time zone.">When</th>
        <th title="The event type. login_* are login failures; lockout_started fires when an account crosses the threshold; signup_* covers the signup flow.">Kind</th>
        <th title="Username supplied with the attempt. For login_unknown_user this is the value the attacker tried, not a real account.">Username</th>
        <th title="Source IP (post-ProxyFix). NULL for events not tied to an HTTP request.">IP</th>
        <th title="Freeform context — failure counter, lockout duration, validation message, etc.">Detail</th>
      </tr>
    </thead>
    <tbody>
      {% for e in events %}
      <tr>
        <td>{{ e.ts_h }}</td>
        <td><span class="kind kind-{{ e.kind }}">{{ e.kind }}</span></td>
        <td>{{ e.username or '' }}</td>
        <td>{% if e.ip %}<code>{{ e.ip }}</code>{% endif %}</td>
        <td>{{ e.detail or '' }}</td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
  <p class="note">Showing the last {{ events|length }} events. Older rows are
    pruned after {{ retention_days }} days.</p>
  {% endif %}"""


# Feedback / error-report ticket queue. A basic ticket system over the
# aime.feedback store: filter by status, read the message (and any captured
# error trace), move a ticket through open → in progress → resolved, and jot a
# triage note. State-changing actions are CSRF-guarded admin POSTs.
_FRAGMENT_FEEDBACK = """
  <h2 title="Feedback and error reports submitted from the chat UI. Triage them here.">Feedback &amp; error reports</h2>

  <div class="userfilter" style="margin-bottom: 1rem;">
    <a class="chipbtn {{ 'active' if not status_filter else '' }}" href="/?{{ qs_all }}">All <span class="note">{{ counts.total }}</span></a>
    <a class="chipbtn {{ 'active' if status_filter == 'open' else '' }}" href="/?{{ qs_open }}">Open <span class="note">{{ counts.open }}</span></a>
    <a class="chipbtn {{ 'active' if status_filter == 'in_progress' else '' }}" href="/?{{ qs_in_progress }}">In progress <span class="note">{{ counts.in_progress }}</span></a>
    <a class="chipbtn {{ 'active' if status_filter == 'resolved' else '' }}" href="/?{{ qs_resolved }}">Resolved <span class="note">{{ counts.resolved }}</span></a>
  </div>

  {% if not tickets %}
    <p class="empty">No {{ status_filter.replace('_', ' ') if status_filter else '' }} tickets.</p>
  {% else %}
  <table>
    <thead>
      <tr>
        <th title="Ticket id.">ID</th>
        <th title="Whether this came from the Send-feedback button or an error report.">Kind</th>
        <th title="Account that submitted it.">User</th>
        <th title="The message, plus any captured error trace.">Message</th>
        <th title="When it was submitted (UTC).">Submitted</th>
        <th title="Triage state.">Status</th>
        <th title="Move the ticket through its lifecycle and jot a note.">Triage</th>
      </tr>
    </thead>
    <tbody>
      {% for t in tickets %}
      <tr>
        <td>#{{ t.id }}</td>
        <td><span class="pill pill-{{ t.kind }}">{{ t.kind }}</span></td>
        <td>{{ t.username or '(unknown)' }}</td>
        <td>
          <p class="ticket-msg">{{ t.message }}</p>
          {% if t.detail %}
          <details class="ticket-detail">
            <summary>{{ 'Error trace' if t.kind == 'error' else 'Details' }}</summary>
            <pre>{{ t.detail }}</pre>
          </details>
          {% endif %}
        </td>
        <td title="{{ t.created_at }} UTC">{{ t.created_at }}</td>
        <td><span class="pill pill-{{ t.status }}">{{ t.status.replace('_', ' ') }}</span></td>
        <td class="actions">
          <form method="post" action="feedback/status" class="inline-action">
            <input type="hidden" name="csrf" value="{{ csrf }}">
            <input type="hidden" name="id" value="{{ t.id }}">
            <input type="hidden" name="status_filter" value="{{ status_filter }}">
            <select name="status" onchange="this.form.submit()" title="Set the ticket status.">
              {% for s in statuses %}
              <option value="{{ s }}" {{ 'selected' if t.status == s else '' }}>{{ s.replace('_', ' ') }}</option>
              {% endfor %}
            </select>
            <noscript><button type="submit">Set</button></noscript>
          </form>
          <form method="post" action="feedback/note" class="ticket-note">
            <input type="hidden" name="csrf" value="{{ csrf }}">
            <input type="hidden" name="id" value="{{ t.id }}">
            <input type="hidden" name="status_filter" value="{{ status_filter }}">
            <textarea name="note" rows="1" placeholder="Triage note…">{{ t.admin_note or '' }}</textarea>
            <button type="submit">Save note</button>
          </form>
        </td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
  {% endif %}"""


# Errors tab. The server-side companion to Feedback: every error Aime itself hit
# (a transient Anthropic outage, a malformed request, an unexpected exception),
# captured automatically with the diagnostic bits that help — exception class,
# HTTP status, Anthropic request-id, model, the user/session, and a traceback.
# Identical errors are folded onto one row with a Count so an outage burst stays
# readable. Triaged like feedback: new → seen → resolved, plus a note. The short
# `reference` is what the user is shown in chat, so a user report lines up here.
_FRAGMENT_ERRORS = """
  <h2 title="Errors Aime hit, captured automatically. Triage them here.">Errors &amp; diagnostics</h2>

  <div class="userfilter" style="margin-bottom: 1rem;">
    <a class="chipbtn {{ 'active' if not status_filter else '' }}" href="/?{{ qs_all }}">All <span class="note">{{ counts.total }}</span></a>
    <a class="chipbtn {{ 'active' if status_filter == 'new' else '' }}" href="/?{{ qs_new }}">New <span class="note">{{ counts.new }}</span></a>
    <a class="chipbtn {{ 'active' if status_filter == 'seen' else '' }}" href="/?{{ qs_seen }}">Seen <span class="note">{{ counts.seen }}</span></a>
    <a class="chipbtn {{ 'active' if status_filter == 'resolved' else '' }}" href="/?{{ qs_resolved }}">Resolved <span class="note">{{ counts.resolved }}</span></a>
  </div>

  {% if not errors %}
    <p class="empty">No {{ status_filter if status_filter else '' }} errors. {% if not status_filter %}Nothing has gone wrong — or nothing has been captured yet.{% endif %}</p>
  {% else %}
  <table>
    <thead>
      <tr>
        <th title="Most recent occurrence (UTC).">Last seen</th>
        <th title="How the error was classified for the user-facing message.">Category</th>
        <th title="The exception class and, where present, the HTTP status.">Error</th>
        <th title="Where it was caught in the pipeline.">Source</th>
        <th title="Anthropic request id — quote this to provider support.">Request id</th>
        <th title="Model in play for the failed turn.">Model</th>
        <th title="Account it happened to.">User</th>
        <th title="How many times this exact error has fired in the dedup window.">Count</th>
        <th title="The error message, with full traceback inside.">Message</th>
        <th title="The reference id shown to the user in chat.">Ref</th>
        <th title="Triage state.">Status</th>
        <th title="Move the row through its lifecycle and jot a note.">Triage</th>
      </tr>
    </thead>
    <tbody>
      {% for e in errors %}
      <tr>
        <td title="{{ e.last_seen }} UTC">{{ e.last_seen }}</td>
        <td><span class="pill pill-{{ e.category }}">{{ e.category }}</span></td>
        <td>{{ e.error_class or '(unknown)' }}{% if e.status_code %} <span class="note">{{ e.status_code }}</span>{% endif %}</td>
        <td>{{ e.source or '' }}</td>
        <td>{% if e.request_id %}<code>{{ e.request_id }}</code>{% else %}<span class="note">—</span>{% endif %}</td>
        <td>{{ e.model or '' }}</td>
        <td>{{ e.username or '(unknown)' }}</td>
        <td>{% if e.count and e.count > 1 %}<strong>{{ e.count }}×</strong>{% else %}{{ e.count }}{% endif %}</td>
        <td>
          <button type="button" class="err-open"
            data-title="{{ e.error_class or 'Error' }}{% if e.status_code %} · {{ e.status_code }}{% endif %} · ref {{ e.reference }}"
            data-message="{{ e.message or '(no message)' }}"
            data-traceback="{{ e.traceback or '' }}"
            title="Click to see the full message and traceback.">{{ (e.message or '(no message)')|truncate(80, True) }}</button>
        </td>
        <td><code>{{ e.reference }}</code></td>
        <td><span class="pill pill-{{ e.status }}">{{ e.status }}</span></td>
        <td class="actions">
          <form method="post" action="errors/status" class="inline-action">
            <input type="hidden" name="csrf" value="{{ csrf }}">
            <input type="hidden" name="id" value="{{ e.id }}">
            <input type="hidden" name="status_filter" value="{{ status_filter }}">
            <select name="status" onchange="this.form.submit()" title="Set the error status.">
              {% for s in statuses %}
              <option value="{{ s }}" {{ 'selected' if e.status == s else '' }}>{{ s }}</option>
              {% endfor %}
            </select>
            <noscript><button type="submit">Set</button></noscript>
          </form>
          <form method="post" action="errors/note" class="ticket-note">
            <input type="hidden" name="csrf" value="{{ csrf }}">
            <input type="hidden" name="id" value="{{ e.id }}">
            <input type="hidden" name="status_filter" value="{{ status_filter }}">
            <textarea name="note" rows="1" placeholder="Triage note…">{{ e.admin_note or '' }}</textarea>
            <button type="submit">Save note</button>
          </form>
        </td>
      </tr>
      {% endfor %}
    </tbody>
  </table>

  <!-- One shared card the row previews open, populated from the clicked
       button's data-* via textContent (no innerHTML, so message/traceback
       text can't inject markup). Admin tabs render server-side on load, so
       this inline script runs; the tab never polls /fragment. -->
  <div id="err-modal" class="err-modal" hidden>
    <div class="err-card" role="dialog" aria-modal="true" aria-labelledby="err-card-title">
      <div class="err-card-head">
        <strong id="err-card-title"></strong>
        <button type="button" class="err-close" aria-label="Close">&times;</button>
      </div>
      <h4>Message</h4>
      <pre id="err-card-message"></pre>
      <h4 id="err-card-tb-head">Traceback</h4>
      <pre id="err-card-tb"></pre>
    </div>
  </div>
  <script>
  (function () {
    var modal = document.getElementById("err-modal");
    if (!modal || modal._wired) return;
    modal._wired = true;
    var title = document.getElementById("err-card-title");
    var msg = document.getElementById("err-card-message");
    var tb = document.getElementById("err-card-tb");
    var tbHead = document.getElementById("err-card-tb-head");
    function openCard(btn) {
      title.textContent = btn.getAttribute("data-title") || "Error";
      msg.textContent = btn.getAttribute("data-message") || "(no message)";
      var t = btn.getAttribute("data-traceback") || "";
      tb.textContent = t;
      tb.hidden = !t;
      tbHead.hidden = !t;
      modal.hidden = false;
    }
    function closeCard() { modal.hidden = true; }
    document.addEventListener("click", function (e) {
      var btn = e.target.closest(".err-open");
      if (btn) { openCard(btn); return; }
      if (e.target === modal || e.target.closest(".err-close")) closeCard();
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && !modal.hidden) closeCard();
    });
  })();
  </script>
  {% endif %}"""


# Billing tab. In billing mode this shows each subscriber's Stripe status as the
# webhook last recorded it (read-only — Stripe is the system of record; see
# aime.billing, docs/billing.md). In keys/open mode it documents the tiers and
# how to switch billing on. Tiers are still assigned by an admin on the Accounts
# tab for keys-mode users; in billing mode the webhook owns tier + api_access.
_FRAGMENT_BILLING = """
  <h2>Billing</h2>
  <p class="empty" style="text-align:left;max-width:60ch">
    Access mode is <code>{{ access_mode }}</code>. A tier's daily <em>cost
    allowance</em> (Aime's internal Anthropic-spend budget — not the customer
    price) is:
  </p>
  <table>
    <thead><tr><th>Tier</th><th>Daily allowance</th><th>Max banked ({{ bank_days }} days)</th></tr></thead>
    <tbody>
      {% for t, cap in tiers.items() %}
      <tr><td>{{ t }}</td><td>${{ '%.2f'|format(cap) }}/day</td><td>${{ '%.2f'|format(cap * bank_days) }}</td></tr>
      {% endfor %}
    </tbody>
  </table>
  {% if billing_mode %}
  <h2 style="margin-top:1.5rem" title="Each user who has started a Stripe checkout, with the subscription status the webhook last recorded.">Subscribers</h2>
  {% if not subscribers %}
    <p class="empty">No subscribers yet.</p>
  {% else %}
  <table>
    <thead><tr><th>ID</th><th>Username</th><th>Tier</th><th>Send access</th><th>Subscription</th><th>Stripe</th></tr></thead>
    <tbody>
      {% for u in subscribers %}
      <tr>
        <td>#{{ u.id }}</td>
        <td>{{ u.username }}</td>
        <td>{{ u.tier }}</td>
        <td class="{{ 'good' if u.api_access else 'bad' }}">{{ 'yes' if u.api_access else 'no' }}</td>
        <td>{{ u.subscription_status or '—' }}</td>
        <td><a href="https://dashboard.stripe.com/customers/{{ u.stripe_customer_id }}" target="_blank" rel="noopener">open ↗</a></td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
  <p class="note" style="max-width:60ch">
    Status is what Stripe last sent via webhook; <strong>Send access</strong> is
    the live <code>api_access</code> the webhook set from it. To change a plan or
    payment, use Stripe (or the user's own billing portal) — this view is
    read-only.
  </p>
  {% endif %}
  {% else %}
  <p class="note" style="max-width:60ch">
    To turn billing on: configure the <code>AIME_STRIPE_*</code> environment
    variables and run with <code>AIME_ACCESS_MODE=billing</code> (see
    docs/billing.md). The webhook then owns <code>api_access</code> + tier per
    subscription. In <code>keys</code> mode, assign tiers manually from the
    <strong>Accounts</strong> tab.
  </p>
  {% endif %}
"""


# Accounts admin. A web equivalent of scripts/access_keys.py (grant/revoke,
# revoke-all) + scripts/manage_users.py (delete/restore/purge). Every form
# carries the session CSRF token.
_FRAGMENT_ACCOUNTS = """
  <h2 title="Every active account. 'Send access' is the api_access flag — whether the user may send messages through the paid model backend.">Active accounts</h2>
  {% if not active_users %}
    <p class="empty">No active accounts.</p>
  {% else %}
  <table>
    <thead>
      <tr>
        <th title="Internal account id.">ID</th>
        <th title="Account username.">Username</th>
        <th title="The api_access flag: whether this user may send messages through the paid model backend.">Send access</th>
        <th title="Usage-limit plan. Sets the daily cost allowance; change it to move the user between tiers.">Tier</th>
        <th title="Remaining budget as a percent of the full bank (the 7-day ceiling), with days banked in parentheses.">Usage</th>
        <th title="Grant/revoke send access, or soft-delete the account.">Actions</th>
      </tr>
    </thead>
    <tbody>
      {% for u in active_users %}
      <tr>
        <td>#{{ u.id }}</td>
        <td>{{ u.username }}</td>
        <td class="{{ 'good' if u.api_access else 'bad' }}">{{ 'yes' if u.api_access else 'no' }}{% if u.comp_access %} <span class="note" title="Complimentary full access — granted by an admin, not billed. Stripe won't revoke it.">comp</span>{% endif %}{% if billing_mode and u.trial_used %} <span class="note" title="No free trial — a (re)subscribe is charged immediately. New accounts get the trial by default; this one used it or was flagged ineligible.">no trial</span>{% endif %}</td>
        <td>
          <form method="post" action="accounts/set-tier" class="inline-action">
            <input type="hidden" name="csrf" value="{{ csrf }}">
            <input type="hidden" name="username" value="{{ u.username }}">
            <select name="tier" onchange="this.form.submit()">
              {% for t in tiers %}
              <option value="{{ t }}" {{ 'selected' if u.tier == t else '' }}>{{ t }}</option>
              {% endfor %}
            </select>
            <noscript><button type="submit">Set</button></noscript>
          </form>
        </td>
        <td class="{{ 'bad' if usage[u.username].over else '' }}" title="Daily cap ${{ '%.2f'|format(usage[u.username].daily_cap) }}; balance ${{ '%.2f'|format(usage[u.username].balance) }}; {{ '%.1f'|format(usage[u.username].days_banked) }} days banked">
          {% if usage[u.username].over %}0% (out){% else %}{{ usage[u.username].pct_full|round|int }}%{% if usage[u.username].days_banked >= 1 %} ({{ '%.1f'|format(usage[u.username].days_banked) }}d){% endif %}{% endif %}
        </td>
        <td class="actions">
          {% if billing_mode %}
          <form method="post" action="accounts/comp">
            <input type="hidden" name="csrf" value="{{ csrf }}">
            <input type="hidden" name="username" value="{{ u.username }}">
            <input type="hidden" name="grant" value="{{ '0' if u.comp_access else '1' }}">
            <button type="submit" title="Complimentary full access: gives this user send access with no subscription, and stops Stripe from revoking them.">{{ 'Remove full access' if u.comp_access else 'Grant full access' }}</button>
          </form>
          <form method="post" action="accounts/trial">
            <input type="hidden" name="csrf" value="{{ csrf }}">
            <input type="hidden" name="username" value="{{ u.username }}">
            <input type="hidden" name="used" value="{{ '0' if u.trial_used else '1' }}">
            <button type="submit" title="Free-trial eligibility. 'Deny free trial' marks this account as having used its trial, so a (re)subscribe is charged immediately. 'Allow free trial' restores it. New signups are eligible by default.">{{ 'Allow free trial' if u.trial_used else 'Deny free trial' }}</button>
          </form>
          {% else %}
          <form method="post" action="accounts/access">
            <input type="hidden" name="csrf" value="{{ csrf }}">
            <input type="hidden" name="username" value="{{ u.username }}">
            <input type="hidden" name="grant" value="{{ '0' if u.api_access else '1' }}">
            <button type="submit">{{ 'Revoke access' if u.api_access else 'Grant access' }}</button>
          </form>
          <form method="post" action="accounts/comp">
            <input type="hidden" name="csrf" value="{{ csrf }}">
            <input type="hidden" name="username" value="{{ u.username }}">
            <input type="hidden" name="grant" value="{{ '0' if u.comp_access else '1' }}">
            <button type="submit" title="Always-allow access: durable send access that an admin grants (it isn't auto-revoked). Granting also resets this user's usage to 100%, so this button doubles as a per-user refill. The daily budget still applies — re-grant to refill.">{{ 'Remove always-allow' if u.comp_access else 'Always-allow + reset' }}</button>
          </form>
          {% endif %}
          <form method="post" action="accounts/delete"
            onsubmit="return confirm('Soft-delete {{ u.username }}? The account is disabled but its data is kept, and it can be restored within the {{ grace_days }}-day grace period.')">
            <input type="hidden" name="csrf" value="{{ csrf }}">
            <input type="hidden" name="username" value="{{ u.username }}">
            <button type="submit" class="danger">Delete</button>
          </form>
        </td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
  {% if billing_mode %}
  <p class="note" style="max-width:70ch">
    Billing mode: <strong>Stripe owns send access</strong> for paying users (a
    subscription grants it, a cancellation/failed payment revokes it).
    <strong>Grant full access</strong> comps a user — send access with no
    subscription, which the webhook won't touch. <strong>Tier</strong> follows
    the subscription's plan for paying users (a manual change here is overwritten
    by their next billing event); for comped or not-yet-subscribed users the tier
    dropdown is authoritative.
  </p>
  {% endif %}
  <form method="post" action="accounts/revoke-all" class="inline-action"
    onsubmit="return confirm('Revoke send access for ALL users? This is the billing-cutover action.')">
    <input type="hidden" name="csrf" value="{{ csrf }}">
    <button type="submit" class="danger">Revoke send access for everyone</button>
    <span class="note">Zeroes api_access for every account (billing cutover).</span>
  </form>
  {% if billing_mode %}
  <form method="post" action="accounts/deny-trial-all" class="inline-action"
    onsubmit="return confirm('Deny a fresh free trial to ALL existing accounts? New signups will still get one.')">
    <input type="hidden" name="csrf" value="{{ csrf }}">
    <button type="submit" class="danger">Deny free trial to everyone</button>
    <span class="note">The other half of the cutover: existing accounts subscribe with no trial; new signups still get one.</span>
  </form>
  {% endif %}
  {% endif %}

  <h2 title="Accounts that have been soft-deleted. Their data is retained until the grace period expires, then a purge can permanently remove them.">Soft-deleted accounts</h2>
  {% if not pending %}
    <p class="empty">No soft-deleted accounts.</p>
  {% else %}
  <table>
    <thead>
      <tr>
        <th title="Internal account id.">ID</th>
        <th title="Account username.">Username</th>
        <th title="When the account was soft-deleted (UTC).">Deleted</th>
        <th title="Whether the grace period has expired. Once past grace, the account can be permanently purged.">Status</th>
        <th title="Restore the account, undoing the soft delete.">Actions</th>
      </tr>
    </thead>
    <tbody>
      {% for p in pending %}
      <tr>
        <td>#{{ p.user.id }}</td>
        <td>{{ p.user.username }}</td>
        <td>{{ p.deleted_at }} ({{ p.days_deleted }}d ago)</td>
        <td class="{{ 'bad' if p.expired else 'warn' }}">
          {%- if p.expired -%}past grace — eligible for purge
          {%- else -%}{{ grace_days - p.days_deleted }}d until purge{%- endif -%}
        </td>
        <td class="actions">
          <form method="post" action="accounts/restore">
            <input type="hidden" name="csrf" value="{{ csrf }}">
            <input type="hidden" name="username" value="{{ p.user.username }}">
            <button type="submit">Restore</button>
          </form>
        </td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
  <form method="post" action="accounts/purge" class="inline-action"
    onsubmit="return confirm('Permanently purge {{ expired_count }} expired account(s)? A final backup zip is written first, then the data is deleted. This cannot be undone.')">
    <input type="hidden" name="csrf" value="{{ csrf }}">
    <button type="submit" class="danger" {{ 'disabled' if not expired_count else '' }}>
      Purge {{ expired_count }} expired account(s)</button>
    <span class="note">Only accounts past the {{ grace_days }}-day grace
      period are purged. A backup is taken first.</span>
  </form>
  {% endif %}"""


# Invite-key admin. A web equivalent of scripts/access_keys.py gen / list /
# revoke-key. Raw keys are shown exactly once, right after generation.
_FRAGMENT_KEYS = """
  {% if flash_keys %}
  <div class="banner good">
    <strong>New invite keys — copy them now, they are not shown again:</strong>
    <ul class="keylist">
      {% for k in flash_keys %}<li><code>{{ k }}</code></li>{% endfor %}
    </ul>
  </div>
  {% endif %}

  <h2 title="Mint single-use invite keys. Each key lets one account gain send access by redeeming it.">Generate invite keys</h2>
  <form method="post" action="keys/gen" class="genform">
    <input type="hidden" name="csrf" value="{{ csrf }}">
    <label>Count
      <input type="number" name="count" value="1" min="1" max="50">
    </label>
    <label>Note (optional)
      <input type="text" name="note" placeholder="e.g. Alice" maxlength="80">
    </label>
    <button type="submit">Generate</button>
  </form>

  <h2 title="Every invite key, newest last. Only the SHA-256 hash is stored — the raw key is shown once at generation.">Invite keys ({{ keys|length }})</h2>
  {% if not keys %}
    <p class="empty">No invite keys yet.</p>
  {% else %}
  <table>
    <thead>
      <tr>
        <th title="A prefix of the key's SHA-256 hash. The raw key is never stored.">Key (hash)</th>
        <th title="Optional label set when the key was generated.">Note</th>
        <th title="When the key was minted.">Created</th>
        <th title="Whether the key has been redeemed, and by whom.">Status</th>
        <th title="Revoke an unredeemed key so it can never be used.">Actions</th>
      </tr>
    </thead>
    <tbody>
      {% for k in keys %}
      <tr>
        <td><code>{{ k.key_hash[:16] }}…</code></td>
        <td>{{ k.note or '—' }}</td>
        <td>{{ k.created_at }}</td>
        <td class="{{ 'good' if k.redeemed else '' }}">
          {%- if k.redeemed -%}
            redeemed by {{ k.redeemed_by_username or '(deleted user)' }} at {{ k.redeemed_at }}
          {%- else -%}unredeemed{%- endif -%}
        </td>
        <td class="actions">
          {% if k.redeemed %}—{% else %}
          <form method="post" action="keys/revoke">
            <input type="hidden" name="csrf" value="{{ csrf }}">
            <input type="hidden" name="key_hash" value="{{ k.key_hash }}">
            <button type="submit" class="danger">Revoke</button>
          </form>
          {% endif %}
        </td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
  {% endif %}"""


_PAGE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Aime admin</title>
  <style>
    :root { color-scheme: light dark; }
    body { font: 15px/1.5 system-ui, sans-serif; margin: 2rem auto; max-width: 1240px; padding: 0 1rem; }
    h1 { margin-bottom: .4rem; }
    h2 { margin: 2rem 0 .5rem; font-size: 1.1rem; }
    .meta { color: #888; margin-bottom: 1rem; }
    .note { color: #888; font-size: .85rem; margin: .3rem 0 1rem; }
    table { border-collapse: collapse; width: 100%; margin-bottom: 1rem; }
    th, td { padding: .45rem .7rem; text-align: right; border-bottom: 1px solid #8884; }
    th:first-child, td:first-child { text-align: left; }
    thead th { border-bottom: 2px solid #8888; }
    tbody tr:hover { background: #8881; }
    tfoot td { font-weight: 600; border-top: 2px solid #8888; }
    .cost { font-variant-numeric: tabular-nums; }
    .empty { color: #888; font-style: italic; }
    .err { color: #d23; }

    /* semantic colors */
    .good { color: #2e9e4f; }
    .warn { color: #c8860a; }
    .bad  { color: #d23; }
    .blue   { color: #2f6fd0; }
    .purple { color: #8a4fd0; }
    td.good, td.warn, td.bad { font-weight: 600; }

    /* header */
    .topbar { display: flex; justify-content: space-between; align-items: baseline; }
    .topbar form { margin: 0; }
    .logout { font-size: .85rem; }

    /* tabs */
    nav.tabs { display: flex; gap: .3rem; border-bottom: 2px solid #8884; margin-bottom: 1rem; flex-wrap: wrap; align-items: stretch; }
    nav.tabs a { padding: .45rem .9rem; text-decoration: none; color: #888;
      border: 1px solid transparent; border-bottom: none; border-radius: 6px 6px 0 0; }
    nav.tabs a:hover { background: #8881; }
    nav.tabs a.active { color: inherit; font-weight: 600;
      border-color: #8884; background: #8881; margin-bottom: -2px; }
    nav.tabs .tab-sep { display: inline-block; width: 1px; background: #8884;
      margin: .25rem .35rem; align-self: stretch; }

    /* User-type badge — short word on the drill-down and By-user table. */
    .badge { display: inline-block; padding: .05rem .55rem; margin-left: .4rem;
      font-size: .72rem; border-radius: 999px; border: 1px solid #8884;
      color: #888; font-weight: 600; vertical-align: middle; }
    .badge-power    { background: #d2336622; border-color: #d2336688; color: #d23; }
    .badge-regular  { background: #2e9e4f22; border-color: #2e9e4f88; color: #2e9e4f; }
    .badge-casual   { background: #c8860a22; border-color: #c8860a88; color: #c8860a; }
    .badge-one-off  { background: #8a4fd022; border-color: #8a4fd088; color: #8a4fd0; }
    .badge-none     { background: #8882; }

    /* Unresolved-ticket count next to the Feedback tab. */
    .tab-badge { display: inline-block; min-width: 1.1rem; padding: 0 .35rem;
      font-size: .7rem; line-height: 1.25rem; text-align: center;
      border-radius: 999px; background: #d2333322; border: 1px solid #d2333388;
      color: #d23; font-weight: 700; vertical-align: middle; }
    /* Ticket kind / status pills on the Feedback tab. */
    .pill { display: inline-block; padding: .05rem .5rem; font-size: .72rem;
      border-radius: 999px; border: 1px solid #8884; color: #888; font-weight: 600; }
    .pill-error    { background: #d2333322; border-color: #d2333388; color: #d23; }
    .pill-feedback { background: #2f6fd022; border-color: #2f6fd088; color: #2f6fd0; }
    .pill-open        { background: #c8860a22; border-color: #c8860a88; color: #c8860a; }
    .pill-in_progress { background: #2f6fd022; border-color: #2f6fd088; color: #2f6fd0; }
    .pill-resolved    { background: #2e9e4f22; border-color: #2e9e4f88; color: #2e9e4f; }
    .ticket-msg { white-space: pre-wrap; word-break: break-word; margin: 0; }
    .ticket-detail { margin: .4rem 0 0; }
    .ticket-detail summary { cursor: pointer; color: #888; font-size: .82rem; }
    .ticket-detail pre { white-space: pre-wrap; word-break: break-word;
      background: #8881; border: 1px solid #8883; border-radius: 6px;
      padding: .5rem .6rem; margin: .4rem 0 0; max-height: 220px; overflow: auto;
      font-size: .78rem; }
    .ticket-note textarea { width: 100%; box-sizing: border-box; min-height: 2.2rem;
      font: inherit; font-size: .82rem; }
    /* Errors tab: clickable one-line message preview that opens a card. */
    .err-open { display: block; max-width: 44ch; text-align: left; cursor: pointer;
      background: none; border: none; padding: 0; font: inherit; color: #2f6fd0;
      white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .err-open:hover { text-decoration: underline; }
    .err-modal { position: fixed; inset: 0; z-index: 1000; padding: 1.2rem;
      display: flex; align-items: center; justify-content: center;
      background: #0008; }
    .err-modal[hidden] { display: none; }
    .err-card { background: Canvas; color: CanvasText; border: 1px solid #8886;
      border-radius: 10px; width: 100%; max-width: 800px; max-height: 84vh;
      overflow: auto; padding: 1rem 1.2rem; box-shadow: 0 12px 40px #0007;
      text-align: left; }
    .err-card-head { display: flex; align-items: center; justify-content: space-between;
      gap: 1rem; margin-bottom: .3rem; }
    .err-card-head strong { font-size: .95rem; word-break: break-word; }
    .err-card h4 { margin: .9rem 0 .25rem; font-size: .72rem; color: #888;
      text-transform: uppercase; letter-spacing: .05em; }
    .err-card pre { white-space: pre-wrap; word-break: break-word; margin: 0;
      background: #8881; border: 1px solid #8883; border-radius: 6px;
      padding: .6rem .7rem; font-size: .8rem; }
    .err-close { background: none; border: none; color: #888; cursor: pointer;
      font-size: 1.5rem; line-height: 1; padding: 0 .2rem; }
    .err-close:hover { color: inherit; }

    /* Used by the session-detail timeline to soften the tool-record rows. */
    tr.dim td { color: #888; }
    .dim { color: #888; }

    form.filter { display: flex; flex-wrap: wrap; gap: .8rem; align-items: end;
      margin-bottom: 1rem; padding: .8rem; border: 1px solid #8884; border-radius: 6px; }
    form.filter label { display: flex; flex-direction: column; font-size: .8rem; color: #888; }
    form.filter input, form.filter select { font: inherit; padding: .25rem .4rem; }
    form.filter button { font: inherit; padding: .3rem .9rem; }
    form.filter .quick-group { display: flex; flex-direction: column;
      font-size: .8rem; color: #888; }
    form.filter .quick { display: flex; gap: .3rem; }
    form.filter .quick button { padding: .25rem .55rem; }

    /* Total / avg-per-user toggle on the Overview tab. */
    .view-toggle { display: flex; gap: .35rem; align-items: center;
      margin: 0 0 .7rem; font-size: .85rem; flex-wrap: wrap; }
    .view-toggle .lbl { color: #888; }
    .view-toggle a { padding: .15rem .6rem; text-decoration: none; color: inherit;
      border: 1px solid #8884; border-radius: 999px; background: transparent; }
    .view-toggle a:hover { background: #8881; }
    .view-toggle a.active { background: #8883; border-color: #8886; font-weight: 600; }
    .view-toggle .note { color: #888; font-size: .8rem; margin-left: .3rem; }

    /* Hourly activity bars (Activity tab). */
    .hour-bars { display: grid; grid-template-columns: repeat(24, 1fr);
      gap: 2px; align-items: end; height: 90px; margin: .4rem 0 .3rem;
      padding: .4rem; border: 1px solid #8884; border-radius: 6px; }
    .hour-bars .bar { background: #2f6fd0; border-radius: 2px 2px 0 0;
      min-height: 1px; position: relative; }
    .hour-bars .bar.empty { background: #8882; min-height: 1px; }
    .hour-axis { display: grid; grid-template-columns: repeat(24, 1fr);
      gap: 2px; padding: 0 .4rem; font-size: .7rem; color: #888;
      text-align: center; font-variant-numeric: tabular-nums; }
    .hour-axis span { overflow: hidden; }

    /* Skinny inline bar (used in the stop-reason table for share %). */
    .inline-bar { display: inline-block; height: .55rem; width: 80px;
      background: #8882; border-radius: 3px; vertical-align: middle;
      margin-left: .4rem; position: relative; overflow: hidden; }
    .inline-bar .fill { display: block; height: 100%; background: #2f6fd0; }

    .cards { display: flex; flex-wrap: wrap; gap: .8rem; margin-bottom: 1rem; }
    .card { flex: 1 1 140px; padding: .7rem .9rem; border: 1px solid #8884;
      border-left-width: 4px; border-radius: 6px; }
    .card .num { font-size: 1.5rem; font-variant-numeric: tabular-nums; }
    .card .lbl { font-size: .8rem; color: #888; }
    .accent-green  { border-left-color: #2e9e4f; }
    .accent-blue   { border-left-color: #2f6fd0; }
    .accent-purple { border-left-color: #8a4fd0; }
    .accent-amber  { border-left-color: #c8860a; }
    .accent-red    { border-left-color: #d23; }

    /* Charts (server-rendered SVG, no JS libraries). */
    .chart-wrap { border: 1px solid #8884; border-radius: 6px;
      padding: .5rem; margin-bottom: 1rem; overflow-x: auto; }
    .chart { display: block; width: 100%; height: auto; max-width: 100%; }
    .legend { display: flex; flex-wrap: wrap; gap: .8rem;
      font-size: .8rem; margin-top: .4rem; color: #888; }
    .legend.col { flex-direction: column; gap: .25rem; align-items: flex-start; }
    .legend .lk { display: inline-flex; align-items: center; gap: .35rem; }
    .legend .sw { display: inline-block; width: 10px; height: 10px;
      border-radius: 2px; }

    .sparkline { vertical-align: middle; }
    td.spark { padding: 0 .3rem; min-width: 130px; }
    .userlink { color: #2f6fd0; text-decoration: none; border-bottom: 1px dotted #2f6fd066; }
    .userlink:hover { border-bottom-style: solid; }

    .two-col { display: grid; grid-template-columns: 2fr 1fr; gap: 1.2rem;
      align-items: start; }
    @media (max-width: 800px) { .two-col { grid-template-columns: 1fr; } }
    .donut-wrap { display: flex; gap: 1rem; align-items: center;
      border: 1px solid #8884; border-radius: 6px; padding: .6rem;
      margin-bottom: 1rem; }
    .donut-wrap svg { flex-shrink: 0; }

    /* Period-over-period KPI cards (Trends tab). */
    .delta { font-size: .8rem; margin-top: .2rem; }
    .delta.up   { color: #d23; }
    .delta.down { color: #2e9e4f; }
    .delta.flat { color: #888; }

    /* Status pill + user chip (Users tab). */
    .status-pill { display: inline-block; padding: .05rem .55rem;
      font-size: .75rem; border-radius: 999px; border: 1px solid #8884; }
    .status-new       { background: #2e9e4f22; border-color: #2e9e4f88; color: #2e9e4f; }
    .status-active    { background: #2f6fd022; border-color: #2f6fd088; color: #2f6fd0; }
    .status-dormant   { background: #d2333322; border-color: #d2333388; color: #d23; }

    .pattern-pill { display: inline-block; padding: .05rem .55rem;
      font-size: .75rem; border-radius: 999px; border: 1px solid #8884;
      color: #888; }
    .pattern-daily      { background: #2e9e4f22; border-color: #2e9e4f88; color: #2e9e4f; }
    .pattern-most-days  { background: #2f6fd022; border-color: #2f6fd088; color: #2f6fd0; }
    .pattern-occasional { background: #c8860a22; border-color: #c8860a88; color: #c8860a; }
    .pattern-once       { background: #8a4fd022; border-color: #8a4fd088; color: #8a4fd0; }
    .pattern-none       { background: #8882; }

    /* Pattern filter chip bar (Users tab). */
    .userfilter { display: flex; flex-wrap: wrap; gap: .35rem;
      align-items: center; margin: .2rem 0 .8rem; }
    .userfilter .lbl { color: #888; font-size: .8rem; margin-right: .4rem; }
    .userfilter .chipbtn { font: inherit; font-size: .8rem;
      padding: .15rem .65rem; border-radius: 999px;
      border: 1px solid #8884; background: transparent; color: inherit;
      cursor: pointer; }
    .userfilter .chipbtn:hover { background: #8881; }
    .userfilter .chipbtn.active { background: #2f6fd022;
      border-color: #2f6fd088; color: #2f6fd0; font-weight: 600; }
    .userfilter .chipbtn .note { color: #888; font-weight: normal; }

    .userchips { display: flex; flex-wrap: wrap; gap: .35rem; margin: 0 0 1rem; }
    .chip { display: inline-block; padding: .15rem .55rem;
      font-size: .8rem; border-radius: 999px; border: 1px solid #8884;
      text-decoration: none; color: inherit; background: #8881; }
    .chip:hover { background: #8883; border-color: #8886; }

    /* Print export — kept off-screen on screen-media, opened by a topbar
       button. Hide the entire chrome (nav, filter form, action buttons,
       refresh footer, the lit-up auto-refresh JS, the back link, anchors)
       so the printed page reads as a clean report rather than a UI capture. */
    .toolbar { display: flex; gap: .5rem; align-items: center; }
    .toolbar form { margin: 0; }
    @media print {
      @page { margin: 14mm 12mm; }
      body { max-width: none; margin: 0; padding: 0; color: #000;
        font-size: 11pt; }
      .topbar, nav.tabs, form.filter, .view-toggle,
      .logout-form, .toolbar, .auto, .flash, .inline-action,
      td.actions, .userlink, a.chip, p > a {
        /* fold most UI affordances; keep core data tables and headings */
      }
      nav.tabs, form.filter, .view-toggle, .logout-form, .toolbar,
      .auto, .flash, .inline-action, td.actions { display: none !important; }
      .topbar h1 { font-size: 16pt; margin: 0 0 .3rem; }
      .topbar form { display: none; }
      a, a:visited { color: #000; text-decoration: none; }
      .card { border-color: #888; box-shadow: none; }
      table { font-size: 9.5pt; page-break-inside: avoid; }
      h2 { page-break-after: avoid; }
      .chart-wrap, .donut-wrap, table, .cards { page-break-inside: avoid; }
      /* Inline-bar fills don't print without explicit colour adjust on most
         engines; force exact rendering so the SVG charts come through. */
      .chart, .sparkline, .inline-bar .fill { -webkit-print-color-adjust: exact;
        print-color-adjust: exact; color-adjust: exact; }
      .print-only { display: block !important; }
      .print-header { margin-bottom: .6rem; }
      .print-header .h { font-size: 14pt; font-weight: 600; }
      .print-header .sub { font-size: 9pt; color: #444; }
    }
    .print-only { display: none; }

    .banner { padding: .6rem .9rem; border-radius: 6px; margin-bottom: 1rem; }
    .banner.good { background: #2e9e4f22; border: 1px solid #2e9e4f88; }
    .banner.warn { background: #c8860a22; border: 1px solid #c8860a88; }
    .banner.bad  { background: #d2333322; border: 1px solid #d2333388; }

    /* flash messages */
    .flash { padding: .5rem .8rem; border-radius: 6px; margin-bottom: .6rem; font-size: .9rem; }
    .flash.ok   { background: #2e9e4f22; border: 1px solid #2e9e4f88; }
    .flash.warn { background: #c8860a22; border: 1px solid #c8860a88; }
    .flash.bad  { background: #d2333322; border: 1px solid #d2333388; }

    /* admin action forms */
    td.actions { display: flex; gap: .4rem; justify-content: flex-end; flex-wrap: wrap; }
    td.actions form { margin: 0; }
    button { font: inherit; padding: .25rem .7rem; cursor: pointer;
      border: 1px solid #8886; border-radius: 5px; background: #8881; color: inherit; }
    button:hover:not(:disabled) { background: #8883; }
    button:disabled { opacity: .5; cursor: default; }
    button.danger { border-color: #d2336688; color: #d23; }
    button.danger:hover:not(:disabled) { background: #d2333322; }
    .inline-action { margin: .2rem 0 1rem; display: flex; gap: .6rem; align-items: center; }
    .inline-action .note { margin: 0; }
    form.genform { display: flex; gap: .8rem; align-items: end; flex-wrap: wrap;
      margin-bottom: 1rem; padding: .8rem; border: 1px solid #8884; border-radius: 6px; }
    form.genform label { display: flex; flex-direction: column; font-size: .8rem; color: #888; }
    form.genform input { font: inherit; padding: .25rem .4rem; }
    .keylist { margin: .4rem 0 0; }
    .keylist code, td code { font-size: .9em; }

    .auto { font-size: .85rem; color: #888; }

    /* Custom tooltips. The `title` attributes in the markup are moved to
       `data-tip` by JS on load (and after each refresh), so the explanatory
       text renders as the styled box below instead of the browser's slow,
       hard-to-read native tooltip. */
    [data-tip] { cursor: help; position: relative; }
    thead th[data-tip] { text-decoration: underline dotted #8887; text-underline-offset: 3px; }
    [data-tip]:hover::after {
      content: attr(data-tip);
      position: absolute; left: 0; top: 100%; margin-top: 4px; z-index: 20;
      width: max-content; max-width: 320px;
      padding: .5rem .65rem;
      font: 13px/1.45 system-ui, sans-serif; font-weight: normal;
      white-space: normal; text-align: left;
      color: #f0f0f0; background: #1f2430;
      border: 1px solid #555c; border-radius: 6px;
      box-shadow: 0 3px 10px #0007;
    }
  </style>
</head>
<body>
  <div class="topbar">
    <h1 title="Aime admin dashboard: usage statistics plus account and invite-key management.">Aime admin</h1>
    <div class="toolbar">
      <button type="button" class="print-btn"
        onclick="window.print()"
        title="Open the system print dialog. The print stylesheet hides the navigation, filter form, and action buttons so the output reads as a clean report. Save as PDF from the dialog if you want a file.">Print</button>
      <form method="post" action="/logout" class="logout-form">
        <input type="hidden" name="csrf" value="{{ csrf }}">
        <button type="submit" class="logout">Log out</button>
      </form>
    </div>
  </div>

  <div class="print-only print-header">
    <div class="h">Aime admin — {{ tab }}</div>
    <div class="sub">
      {% if since_raw or until_raw %}window: {{ since_raw or 'start' }} → {{ until_raw or 'now' }} ({{ tz_label or 'UTC' }}){% else %}window: all time ({{ tz_label or 'UTC' }}){% endif %}
      {% if user_raw %} &middot; user: {{ user_raw }}{% endif %}
      {% if model_raw %} &middot; model: {{ model_raw }}{% endif %}
      {% if purpose_raw %} &middot; purpose: {{ purpose_raw }}{% endif %}
    </div>
  </div>

  <nav class="tabs">
    <a href="/?{{ qs_users }}" class="{{ 'active' if tab == 'users' else '' }}"
      title="Who is using Aime — profiles, behavior patterns, engagement classification.">Users</a>
    <a href="/?{{ qs_conversations }}" class="{{ 'active' if tab == 'conversations' else '' }}"
      title="One row per conversation. Sortable by cost — answers 'what kinds of conversations are happening, and which ones are expensive?'.">Conversations</a>
    <a href="/?{{ qs_overview }}" class="{{ 'active' if tab == 'overview' else '' }}"
      title="The top-line $ spend view — totals, deltas vs prior period, by-user and daily summary.">Costs</a>
    <span class="tab-sep"></span>
    <a href="/?{{ qs_cache }}" class="{{ 'active' if tab == 'cache' else '' }}"
      title="Whether prompt caching is paying for itself — reuse factors, hypothetical no-cache cost, 5m-TTL warnings.">Cache</a>
    <a href="/?{{ qs_activity }}" class="{{ 'active' if tab == 'activity' else '' }}"
      title="Call purpose, stop reasons, latency percentiles, hour-of-day shape.">Activity</a>
    <a href="/?{{ qs_tools }}" class="{{ 'active' if tab == 'tools' else '' }}"
      title="Per-tool cost — which tool is worth trimming first.">Tools</a>
    <a href="/?{{ qs_agents }}" class="{{ 'active' if tab == 'agents' else '' }}"
      title="What background-agent runs cost and do, broken down per agent — and their share of total spend.">Agents</a>
    <a href="/?{{ qs_routing }}" class="{{ 'active' if tab == 'routing' else '' }}"
      title="Per-turn Haiku/Sonnet routing — net cost saved vs always-Sonnet, after subtracting classifier overhead.">Routing</a>
    <a href="/?{{ qs_trends }}" class="{{ 'active' if tab == 'trends' else '' }}"
      title="Anomaly day flags and the top-N most expensive turns.">Trends</a>
    <span class="tab-sep"></span>
    <a href="/?{{ qs_accounts }}" class="{{ 'active' if tab == 'accounts' else '' }}"
      title="List, grant/revoke, soft-delete, restore and purge accounts.">Accounts</a>
    <a href="/?{{ qs_keys }}" class="{{ 'active' if tab == 'keys' else '' }}"
      title="Mint and revoke invite keys.">Keys</a>
    <a href="/?{{ qs_billing }}" class="{{ 'active' if tab == 'billing' else '' }}"
      title="Subscription billing — Stripe status per subscriber (read-only) and the tier allowances.">Billing</a>
    <a href="/?{{ qs_system }}" class="{{ 'active' if tab == 'system' else '' }}"
      title="Operator health — account/key counts, storage per user, log size.">System</a>
    <a href="/?{{ qs_security }}" class="{{ 'active' if tab == 'security' else '' }}"
      title="Audit log of failed logins, lockouts, and signup throttles.">Security</a>
    <a href="/?{{ qs_feedback }}" class="{{ 'active' if tab == 'feedback' else '' }}"
      title="User-submitted feedback and error reports — a basic ticket queue.">Feedback{% if feedback_open %} <span class="tab-badge">{{ feedback_open }}</span>{% endif %}</a>
    <a href="/?{{ qs_errors }}" class="{{ 'active' if tab == 'errors' else '' }}"
      title="Errors Aime captured automatically — transient outages, bad requests, exceptions.">Errors{% if errors_open %} <span class="tab-badge">{{ errors_open }}</span>{% endif %}</a>
  </nav>

  {% for f in flashes %}
  <div class="flash {{ f.level }}">{{ f.msg }}</div>
  {% endfor %}

  {% if tab in ('overview', 'cache', 'activity', 'tools', 'agents', 'trends', 'users', 'routing', 'conversations') %}
  <form class="filter" method="get">
    <input type="hidden" name="tab" value="{{ tab }}">
    <label title="Only include records on or after this date, interpreted in the selected time zone. Accepts YYYY-MM-DD or a full ISO-8601 timestamp. Leave blank for no lower bound.">Since ({{ tz_label or 'UTC' }})
      <input type="text" name="since" value="{{ since_raw }}" placeholder="YYYY-MM-DD">
    </label>
    <label title="Only include records on or before this date, interpreted in the selected time zone. A bare YYYY-MM-DD covers the whole day in that zone (through 23:59:59). Leave blank for no upper bound.">Until ({{ tz_label or 'UTC' }})
      <input type="text" name="until" value="{{ until_raw }}" placeholder="YYYY-MM-DD">
    </label>
    <div class="quick-group" title="Quick presets that fill the Since / Until fields and apply immediately."><span>Quick range</span>
      <span class="quick">
        <button type="button" title="Today only." onclick="quickRange(0)">Today</button>
        <button type="button" title="Today and the previous 6 days." onclick="quickRange(7)">7d</button>
        <button type="button" title="Today and the previous 29 days." onclick="quickRange(30)">30d</button>
        <button type="button" title="Month-to-date — from the first of this UTC month through today." onclick="quickRangeMTD()">MTD</button>
        <button type="button" title="Year-to-date — from January 1 through today." onclick="quickRangeYTD()">YTD</button>
        <button type="button" title="Clear both date bounds — all-time view." onclick="quickRange(null)">All</button>
      </span>
    </div>
    <label title="Restrict every table to a single user. (anonymous) covers records logged without a username.">User
      <select name="user">
        <option value="">(all users)</option>
        {% for name in all_users %}
        <option value="{{ name }}" {{ 'selected' if name == user_raw else '' }}>{{ name }}</option>
        {% endfor %}
      </select>
    </label>
    <label title="Restrict every table to a single model id (as stamped on the record, e.g. claude-sonnet-4-6). (unknown) covers records with no model.">Model
      <select name="model">
        <option value="">(all models)</option>
        {% for name in all_models %}
        <option value="{{ name }}" {{ 'selected' if name == model_raw else '' }}>{{ name }}</option>
        {% endfor %}
      </select>
    </label>
    <label title="Restrict every table to a single call purpose (turn / title / compaction / ...). (unspecified) covers records that pre-date the tag.">Purpose
      <select name="purpose">
        <option value="">(all purposes)</option>
        {% for name in all_purposes %}
        <option value="{{ name }}" {{ 'selected' if name == purpose_raw else '' }}>{{ name }}</option>
        {% endfor %}
      </select>
    </label>
    <label title="Time zone used to interpret the Since/Until filters and to bucket records by day and hour-of-day. UTC is the safe default for shared deployments; Auto reads the browser's IANA zone via JavaScript and uses it as the dashboard's display zone.">Time zone
      <select name="tz">
        <option value="auto" {{ 'selected' if tz_raw == 'auto' else '' }}>Auto (browser)</option>
        {% for name in tz_options %}
        <option value="{{ name }}" {{ 'selected' if tz_raw == name or (not tz_raw and name == 'UTC') else '' }}>{{ name }}</option>
        {% endfor %}
        {% if tz_raw and tz_raw != 'auto' and tz_raw not in tz_options %}
        <option value="{{ tz_raw }}" selected>{{ tz_raw }}</option>
        {% endif %}
      </select>
    </label>
    <label title="How often the figures reload in place. The data region updates without disturbing this form or your scroll position.">Auto-refresh
      <select name="auto">
        <option value="0" {{ 'selected' if auto == 0 else '' }}>off</option>
        <option value="1" {{ 'selected' if auto == 1 else '' }}>1s</option>
        <option value="30" {{ 'selected' if auto == 30 else '' }}>30s</option>
        <option value="300" {{ 'selected' if auto == 300 else '' }}>5m</option>
      </select>
    </label>
    <button type="submit" title="Apply the filters above and reload the data.">Apply</button>
    <a href="{{ request.path }}?tab={{ tab }}" title="Clear all filters and return to the all-time view on this tab.">reset</a>
  </form>
  <p class="note" title="Dates and timestamps throughout the dashboard — the Since/Until filters, the By day grouping, and log timestamps — are interpreted in the selected time zone. Pick UTC for a deploy-wide view, a named zone to anchor a specific region, or Auto to follow the browser's IANA zone.">All dates and times shown are <strong>{{ tz_label or 'UTC' }}</strong>.</p>
  {% endif %}

  <div id="data">{{ fragment|safe }}</div>

  <script>
    // Move every `title` to `data-tip` so the CSS tooltip box renders instead
    // of the browser's native tooltip (which would otherwise show on top of
    // it). Re-run after each refresh, since the swapped-in fragment HTML
    // arrives with fresh `title` attributes.
    function dressTooltips(root) {
      var els = root.querySelectorAll("[title]");
      for (var i = 0; i < els.length; i++) {
        els[i].setAttribute("data-tip", els[i].getAttribute("title"));
        els[i].removeAttribute("title");
      }
    }
    dressTooltips(document);

    // Pattern chip filter on the Users tab — uses event delegation on
    // document so the listener survives every fragment refresh (the auto-
    // refresh replaces #data's innerHTML, which would otherwise wipe any
    // listener attached to a child element inside the fragment).
    document.addEventListener("click", function (e) {
      var b = e.target.closest(".userfilter button[data-pattern]");
      if (!b) return;
      var bar = b.parentElement;
      bar.querySelectorAll(".chipbtn").forEach(function (x) {
        x.classList.remove("active");
      });
      b.classList.add("active");
      var p = b.getAttribute("data-pattern");
      document.querySelectorAll("#userstable tbody tr").forEach(function (r) {
        var show = (
          p === "all" ||
          r.getAttribute("data-pattern") === p ||
          (p === "heavy" && r.getAttribute("data-heavy") === "1") ||
          (p === "dormant" && r.getAttribute("data-status") === "dormant")
        );
        r.style.display = show ? "" : "none";
      });
    });

    // When the tz select is "auto", resolve it to the browser's IANA zone
    // before the filter form submits so the server sees a concrete name.
    // Quick-range presets and the regular Apply submit both flow through here.
    (function () {
      var form = document.querySelector("form.filter");
      if (!form) return;
      form.addEventListener("submit", function () {
        var tz = form.elements["tz"];
        if (tz && tz.value === "auto") {
          try {
            var detected = Intl.DateTimeFormat().resolvedOptions().timeZone;
            if (detected) {
              // Carry the original mode as a hidden field so the next render
              // can re-select "Auto" in the dropdown rather than the resolved
              // name — keeps the choice sticky across navigations.
              var mode = document.createElement("input");
              mode.type = "hidden";
              mode.name = "tz";
              mode.value = detected;
              form.appendChild(mode);
              tz.disabled = true;
              var flag = document.createElement("input");
              flag.type = "hidden";
              flag.name = "tz_auto";
              flag.value = "1";
              form.appendChild(flag);
            }
          } catch (e) { /* leave tz=auto; server falls back to UTC */ }
        }
      });
    })();

    // Quick-range presets: fill the Since/Until fields and submit the filter
    // form. `days` is the inclusive window size (0 = today only); null clears
    // both bounds for the all-time view. Dates are local-time YYYY-MM-DD,
    // matching what _parse_bound expects.
    function quickRange(days) {
      var form = document.querySelector("form.filter");
      var since = form.elements["since"], until = form.elements["until"];
      if (days === null) {
        since.value = "";
        until.value = "";
      } else {
        var fmt = function (d) {
          return d.getFullYear() + "-" +
            String(d.getMonth() + 1).padStart(2, "0") + "-" +
            String(d.getDate()).padStart(2, "0");
        };
        var end = new Date();
        var start = new Date();
        start.setDate(start.getDate() - Math.max(0, days - 1));
        since.value = fmt(start);
        until.value = fmt(end);
      }
      form.submit();
    }
    function quickRangeMTD() {
      var form = document.querySelector("form.filter");
      var d = new Date();
      var fmt = function (x) {
        return x.getFullYear() + "-" +
          String(x.getMonth() + 1).padStart(2, "0") + "-" +
          String(x.getDate()).padStart(2, "0");
      };
      var first = new Date(d.getFullYear(), d.getMonth(), 1);
      form.elements["since"].value = fmt(first);
      form.elements["until"].value = fmt(d);
      form.submit();
    }
    function quickRangeYTD() {
      var form = document.querySelector("form.filter");
      var d = new Date();
      var fmt = function (x) {
        return x.getFullYear() + "-" +
          String(x.getMonth() + 1).padStart(2, "0") + "-" +
          String(x.getDate()).padStart(2, "0");
      };
      var first = new Date(d.getFullYear(), 0, 1);
      form.elements["since"].value = fmt(first);
      form.elements["until"].value = fmt(d);
      form.submit();
    }
  </script>

  {% if auto %}
  <p class="auto">Live — figures refresh every {{ auto_label }}.</p>
  <script>
    // Re-fetch only the data region on the chosen interval and swap it in
    // place. The filter form (and any field the user is editing) lives
    // outside #data, so it is never touched — no focus loss, no scroll jump.
    var qs = window.location.search;
    setInterval(function () {
      fetch("fragment" + qs, { headers: { "X-Requested-With": "fetch" } })
        .then(function (r) { return r.ok ? r.text() : null; })
        .then(function (html) {
          if (html === null) return;
          var data = document.getElementById("data");
          data.innerHTML = html;
          dressTooltips(data);
        })
        .catch(function () { /* transient failure — try again next tick */ });
    }, {{ auto }} * 1000);
  </script>
  {% endif %}
</body>
</html>"""


_LOGIN_PAGE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Aime admin — sign in</title>
  <style>
    :root { color-scheme: light dark; }
    body { font: 15px/1.5 system-ui, sans-serif; min-height: 100vh; margin: 0;
      display: flex; align-items: center; justify-content: center; }
    form { display: flex; flex-direction: column; gap: .8rem; width: 280px;
      padding: 1.6rem; border: 1px solid #8884; border-radius: 10px; }
    h1 { margin: 0; font-size: 1.3rem; }
    .sub { margin: 0; color: #888; font-size: .88rem; }
    input { font: inherit; padding: .5rem .6rem; border: 1px solid #8886;
      border-radius: 6px; background: transparent; color: inherit; }
    button { font: inherit; font-weight: 600; padding: .5rem; cursor: pointer;
      border: 1px solid #8886; border-radius: 6px; background: #8881; color: inherit; }
    button:hover { background: #8883; }
    .err { color: #d23; font-size: .85rem; margin: 0; }
  </style>
</head>
<body>
  <form method="post">
    <h1>Aime admin</h1>
    <p class="sub">Enter the admin password to manage usage, accounts, and keys.</p>
    {% if error %}<p class="err">{{ error }}</p>{% endif %}
    <input type="password" name="password" placeholder="Admin password" autofocus required>
    <button type="submit">Sign in</button>
  </form>
</body>
</html>"""

_AUTO_LABELS = {1: "second", 30: "30 seconds", 300: "5 minutes"}


def _refresh_seconds(args) -> int:
    """Validated auto-refresh interval. Unknown values fall back to default."""
    try:
        val = int(args.get("auto", _REFRESH_DEFAULT))
    except (TypeError, ValueError):
        return _REFRESH_DEFAULT
    return val if val in _REFRESH_CHOICES else _REFRESH_DEFAULT


def _aggregate_tiers(records, user_tiers):
    """Tier-fit analytics from the windowed api records, joined with each user's
    current tier (``user_tiers``: username -> tier).

    Answers the tier-system questions: how much of each tier's daily allowance
    its users actually consume, how many exceed it, and how often. The
    comparison is against the **daily cap** (not the banked bucket): for tier
    *sizing*, "on what fraction of active days does a user blow past the daily
    allowance?" is the useful signal. (A day over the cap isn't necessarily a
    blocked day — the bucket banks several days — but a high over-rate says the
    tier is undersized for that cohort.)

    Users in the log but not in ``user_tiers`` (e.g. since-deleted) bucket under
    "(unknown)". Tier is the user's *current* tier; mid-window changes are not
    back-applied.
    """
    # Cost per (user, day).
    ud: dict = {}
    for rec in records:
        if rec.get("kind") != "api":
            continue
        user = rec.get("user") or "(anonymous)"
        day = str(rec.get("ts", ""))[:10]
        if not day:
            continue
        ud[(user, day)] = ud.get((user, day), 0.0) + _report._api_cost(rec)

    tiers: dict = {}
    for (user, day), cost in ud.items():
        tier = user_tiers.get(user, "(unknown)")
        cap = _config.tier_daily_cap(tier) if tier in _config.USAGE_TIERS else 0.0
        t = tiers.setdefault(tier, {
            "users": set(), "user_days": 0, "total_cost": 0.0,
            "over_days": 0, "users_over": set(), "util_sum": 0.0, "max_day": 0.0,
            "cap": cap,
        })
        t["users"].add(user)
        t["user_days"] += 1
        t["total_cost"] += cost
        t["max_day"] = max(t["max_day"], cost)
        if cap > 0:
            t["util_sum"] += cost / cap
            if cost > cap:
                t["over_days"] += 1
                t["users_over"].add(user)

    # Tier populations (so the user count reflects everyone on a tier, not just
    # those active in the window).
    population: dict = {}
    for user, tier in user_tiers.items():
        population.setdefault(tier, set()).add(user)

    rows = []
    seen = set()
    for tier, t in tiers.items():
        seen.add(tier)
        ud_n = t["user_days"]
        rows.append({
            "tier": tier,
            "cap": t["cap"],
            "n_users": len(population.get(tier, set())) or len(t["users"]),
            "n_active": len(t["users"]),
            "avg_daily": (t["total_cost"] / ud_n) if ud_n else 0.0,
            "avg_util_pct": (100.0 * t["util_sum"] / ud_n) if ud_n else 0.0,
            "users_over": len(t["users_over"]),
            "over_days": t["over_days"],
            "user_days": ud_n,
            "over_rate_pct": (100.0 * t["over_days"] / ud_n) if ud_n else 0.0,
            "max_day": t["max_day"],
        })
    # Tiers with population but no activity in the window still get a row.
    for tier, members in population.items():
        if tier in seen:
            continue
        rows.append({
            "tier": tier, "cap": _config.tier_daily_cap(tier),
            "n_users": len(members), "n_active": 0, "avg_daily": 0.0,
            "avg_util_pct": 0.0, "users_over": 0, "over_days": 0,
            "user_days": 0, "over_rate_pct": 0.0, "max_day": 0.0,
        })
    # Configured tier order first (light, power, ...), unknown/extras last.
    order = list(_config.USAGE_TIERS.keys())
    rows.sort(key=lambda r: (order.index(r["tier"]) if r["tier"] in order
                             else len(order), r["tier"]))
    return rows


def _compute(args):
    """Build the template context for the current filter query args."""
    path = _log_path()

    since_raw = args.get("since", "").strip()
    until_raw = args.get("until", "").strip()
    user_raw = args.get("user", "").strip()
    model_raw = args.get("model", "").strip()
    purpose_raw = args.get("purpose", "").strip()
    tz_raw = args.get("tz", "").strip()
    tz_auto = args.get("tz_auto") == "1"

    since, since_err = _parse_bound(since_raw, end=False)
    until, until_err = _parse_bound(until_raw, end=True)
    errors = [e for e in (since_err, until_err) if e]

    zone, tz_label = _resolve_zone(tz_raw)
    # When the JS auto-detect resolved a real zone, the form still wants the
    # dropdown to show "Auto" rather than the resolved name — keeps the user's
    # original intent sticky across navigation.
    tz_display = "auto" if tz_auto else tz_raw

    # Load the whole log once so the dropdowns can list every user / model /
    # purpose ever seen, not just those that the current filter would keep. A
    # bad date is treated as "no bound".
    all_records = []
    if os.path.exists(path):
        all_records = list(_report.load_records(path, None, None, None))
    if zone is not None:
        # Records on disk are naive UTC; shift each `ts` into the selected
        # display zone so every downstream aggregation that slices by day or
        # hour-of-day reads the user's local clock. The since/until bounds are
        # already-typed local values from _parse_bound, so they line up.
        _shift_records_to_zone(all_records, zone)
    now_local = _local_now(zone)
    all_users = sorted({r.get("user") or "(anonymous)" for r in all_records})
    all_models = sorted({r.get("model") or "(unknown)"
                         for r in all_records if r.get("kind") == "api"})
    all_purposes = sorted({r.get("purpose") or "(unspecified)"
                           for r in all_records if r.get("kind") == "api"})

    # Apply the window / user / model / purpose filter in Python against the
    # already-loaded set.
    def _keep(rec):
        try:
            ts = datetime.datetime.fromisoformat(rec["ts"])
        except (ValueError, KeyError):
            return False
        if since and ts < since:
            return False
        if until and ts > until:
            return False
        if user_raw and (rec.get("user") or "(anonymous)") != user_raw:
            return False
        if model_raw and (rec.get("model") or "(unknown)") != model_raw:
            return False
        # The Purpose filter only applies to api records, which are the only
        # records that carry a `purpose` field. Applying it to tool / stt
        # records would silently empty the Tools tab whenever Purpose is set.
        if purpose_raw and rec.get("kind") == "api" and (
            (rec.get("purpose") or "(unspecified)") != purpose_raw
        ):
            return False
        return True

    records = [r for r in all_records if _keep(r)]

    # Tier-fit analytics: each active user's current tier (from auth) joined
    # against their per-day spend. Best-effort — a missing auth backend just
    # yields an empty mapping (every user buckets under "(unknown)").
    user_tiers: dict = {}
    try:
        for u in _auth_backend().list_users():
            user_tiers[u.username] = u.tier
    except Exception:
        pass
    tier_rows = _aggregate_tiers(records, user_tiers)

    # --- Overview aggregations ---
    users = _report.aggregate(records)
    grand_cost = sum(u["cost"] for u in users.values())
    total_calls = sum(u["api_calls"] for u in users.values())
    total_in = sum(u["input"] for u in users.values())
    total_out = sum(u["output"] for u in users.values())
    total_cache_r = sum(u["cache_r"] for u in users.values())
    # Share of read-side tokens served from cache rather than billed as fresh
    # input — a quick read on how well prompt caching is working.
    denom = total_in + total_cache_r
    cache_hit_pct = (100.0 * total_cache_r / denom) if denom else 0.0

    # `user_count` is the divisor for the avg-per-user view — every distinct
    # username that actually has a record in this window, including
    # `(anonymous)` if any records were logged without linkage. Avoids dividing
    # by an account roster that has never sent a message.
    user_count = len(users)
    view = "avg" if args.get("view") == "avg" else "total"
    if view == "avg" and user_count:
        card_cost = grand_cost / user_count
        card_calls = total_calls / user_count
        card_tokens = (total_in + total_out) / user_count
    else:
        card_cost = grand_cost
        card_calls = total_calls
        card_tokens = total_in + total_out

    by_day = sorted(_aggregate_by_day(records).items(), reverse=True)
    by_model = sorted(_aggregate_by_model(records).items(),
                      key=lambda kv: kv[1]["cost"], reverse=True)

    # --- Activity aggregations ---
    purposes = _aggregate_purpose(records)
    purpose_rows = sorted(purposes.items(), key=lambda kv: kv[1]["cost"], reverse=True)
    stop_counts, stop_total = _aggregate_stop_reasons(records)
    stop_rows = sorted(stop_counts.items(), key=lambda kv: kv[1], reverse=True)
    hours = _aggregate_hour(records)
    hour_max = max(hours) if hours else 0
    lats = _overall_latency(records)
    lat_n = len(lats)
    lat_avg = (sum(lats) / lat_n) if lat_n else None
    lat_p50 = _percentile(lats, 50)
    lat_p90 = _percentile(lats, 90)
    lat_p99 = _percentile(lats, 99)

    # --- Cache-efficacy aggregations ---
    cache_users_map = _aggregate_cache(records)
    cache_with = sum(u["with_cache"] for u in cache_users_map.values())
    cache_without = sum(u["without_cache"] for u in cache_users_map.values())
    cache_savings = cache_without - cache_with
    cache_savings_pct = (100.0 * cache_savings / cache_without) if cache_without else 0.0
    total_writes = sum(u["writes"] for u in cache_users_map.values())
    total_reads = sum(u["reads"] for u in cache_users_map.values())
    cache_reuse = (total_reads / total_writes) if total_writes else 0.0
    flagged = sorted(n for n, u in cache_users_map.items() if u["ttl_risk"])
    # Heaviest no-cache cost first — that is where caching matters most.
    cache_users = sorted(cache_users_map.items(),
                         key=lambda kv: kv[1]["without_cache"], reverse=True)

    window = "all time"
    if since_raw or until_raw:
        window = f"{since_raw or 'start'} → {until_raw or 'now'}"

    # --- Chart-shaped series, all aligned to the same dense day axis ---
    day_keys = _day_keys_in_range(records)
    daily_cost = _aggregate_cost_per_day(records, day_keys)
    daily_calls = _aggregate_calls_per_day(records, day_keys)
    daily_active = _aggregate_active_users_per_day(records, day_keys)
    daily_savings = _aggregate_cache_savings_per_day(records, day_keys)
    by_day_model = _aggregate_by_day_model(records, day_keys)
    by_day_purpose = _aggregate_purpose_per_day(records, day_keys)
    lat_p50_day, lat_p90_day = _latency_per_day(records, day_keys)

    # Cap visible series to the top 6; lump the rest as "other" so the legend
    # stays readable on a wide log with dozens of obscure models.
    def _top_n_series(series, n=6):
        if len(series) <= n:
            return series
        top = series[:n]
        rest = series[n:]
        if not rest:
            return top
        other = [0.0] * len(day_keys)
        for _name, vs in rest:
            for i, v in enumerate(vs):
                other[i] += v
        return top + [("other", other)]

    by_day_model_top = _top_n_series(by_day_model)
    by_day_purpose_top = _top_n_series(by_day_purpose)

    user_spark = _per_user_sparkline_data(records, day_keys)
    model_spark = _per_model_sparkline_data(records, day_keys)

    # Render the SVG strings server-side so the templates stay simple.
    chart_daily_cost = _svg_line_chart(
        day_keys, [("daily cost", daily_cost)],
        y_label="USD", money=True,
    )
    chart_daily_calls = _svg_line_chart(
        day_keys,
        [("API calls", daily_calls), ("active users", daily_active)],
        y_label="count",
    )
    chart_model_stack = _svg_stacked_bars(
        day_keys, by_day_model_top, money=True,
    )
    chart_purpose_stack = _svg_stacked_bars(
        day_keys, by_day_purpose_top,
    )
    chart_cache_savings = _svg_line_chart(
        day_keys, [("cache savings", daily_savings)],
        y_label="USD", money=True,
        colors=["#2e9e4f"],
    )
    chart_latency_day = _svg_line_chart(
        day_keys, [("p50", lat_p50_day), ("p90", lat_p90_day)],
        y_label="ms",
    )
    chart_hours = _svg_hour_bars(hours, tz_label=tz_label)

    # Rich per-day summary + 7-day rolling-mean overlay on the daily cost
    # chart, so a single weekly spike doesn't dominate the headline trend.
    daily_rows = _daily_summary(records, day_keys)
    rolling_cost = _rolling_average(daily_cost, window=7)
    chart_daily_cost_smoothed = _svg_line_chart(
        day_keys,
        [("daily", daily_cost), ("7-day avg", rolling_cost)],
        y_label="USD", money=True,
        colors=["#2f6fd0", "#c8860a"],
    )

    # Weekday × hour heatmap for the Activity tab. Records are taken from the
    # current filter window, not the full log — admins typically want to see
    # the pattern for the same window the rest of the page describes.
    weekday_labels = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    hour_labels = [f"{h:02d}" for h in range(24)]
    heatmap_grid = _weekday_hour_heatmap(records)
    chart_heatmap = _svg_heatmap(
        heatmap_grid, row_labels=weekday_labels, col_labels=hour_labels,
        title=f"weekday × hour ({tz_label})",
    )

    # Donut: cost share by model in this window.
    model_total = [
        (name, m["cost"], _color_for(i))
        for i, (name, m) in enumerate(by_model)
    ]
    chart_model_donut = _svg_donut(model_total[:8])

    # Sparkline strings keyed by user / model — used by table templates.
    user_sparklines = {n: _svg_sparkline(vs) for n, vs in user_spark.items()}
    model_sparklines = {n: _svg_sparkline(vs) for n, vs in model_spark.items()}

    # --- Tools tab aggregations ---
    # By-tool table, daily stacked bar of cost-by-tool, and a donut of total
    # share. The estimate ranks "which tool should I trim first?"; see
    # _tool_record_cost for the attribution.
    by_tool_map = _aggregate_by_tool(records)
    by_tool = sorted(by_tool_map.items(), key=lambda kv: kv[1]["cost"], reverse=True)
    by_day_tool = _aggregate_tool_per_day(records, day_keys)
    by_day_tool_top = _top_n_series(by_day_tool)
    chart_tool_stack = _svg_stacked_bars(
        day_keys, by_day_tool_top, money=True,
    )
    tool_slices = [
        (name, t["cost"], _color_for(i))
        for i, (name, t) in enumerate(by_tool)
        if t["cost"] > 0
    ]
    chart_tool_donut = _svg_donut(tool_slices[:8])
    tool_sparklines = {
        name: _svg_sparkline(vs) for name, vs in by_day_tool
    }
    tool_total_cost = sum(t["cost"] for _n, t in by_tool)
    tool_total_calls = sum(t["calls"] for _n, t in by_tool)
    tool_top_name, tool_top_cost = (by_tool[0][0], by_tool[0][1]["cost"]) if by_tool else ("—", 0.0)

    # --- Agents tab aggregations ---
    # Cost/usage of headless background-agent runs (source=="agent"), broken
    # down per *user* (not per agent — agent names are unbounded), plus a daily
    # cost stack and per-user sparklines. The cost basis matches Overview (real
    # api cost), so agent_total_cost / the grand total is a true "share of spend
    # driven by agents".
    agents_map = _aggregate_agents(records)
    agents = sorted(agents_map.items(), key=lambda kv: kv[1]["cost"], reverse=True)
    agent_total_cost = sum(a["cost"] for _n, a in agents)
    agent_total_calls = sum(a["api_calls"] for _n, a in agents)
    agent_total_tool_calls = sum(a["tool_calls"] for _n, a in agents)
    agent_total_runs = sum(a["runs"] for _n, a in agents)
    agent_cost_share = (100.0 * agent_total_cost / grand_cost) if grand_cost else 0.0
    agent_top_name, agent_top_cost = (
        (agents[0][0], agents[0][1]["cost"]) if agents else ("—", 0.0)
    )
    by_day_agent = _aggregate_agent_per_day(records, day_keys)
    by_day_agent_top = _top_n_series(by_day_agent)
    chart_agent_stack = _svg_stacked_bars(day_keys, by_day_agent_top, money=True)
    agent_slices = [
        (name, a["cost"], _color_for(i))
        for i, (name, a) in enumerate(agents)
        if a["cost"] > 0
    ]
    chart_agent_donut = _svg_donut(agent_slices[:8])
    agent_sparklines = {name: _svg_sparkline(vs) for name, vs in by_day_agent}

    # --- Model Routing tab aggregations ---
    # Sums savings from Haiku-routed turns (vs counterfactual Sonnet cost on
    # the same token counts), subtracts the classifier-call overhead, and
    # tallies misclassification hints. Renders empty-state gracefully when
    # the log carries no `routed_decision` field yet (pre-routing records or
    # routing disabled).
    routing_users_map = _aggregate_routing(records)
    routing_users = sorted(
        routing_users_map.items(),
        key=lambda kv: kv[1]["net_savings"],
        reverse=True,
    )
    routing_total = {
        "haiku_turns": sum(u["haiku_turns"] for _n, u in routing_users),
        "sonnet_turns": sum(u["sonnet_turns"] for _n, u in routing_users),
        "haiku_actual": sum(u["haiku_actual"] for _n, u in routing_users),
        "haiku_counterfactual": sum(u["haiku_counterfactual"] for _n, u in routing_users),
        "haiku_savings": sum(u["haiku_savings"] for _n, u in routing_users),
        "router_calls": sum(u["router_calls"] for _n, u in routing_users),
        "router_cost": sum(u["router_cost"] for _n, u in routing_users),
        "maybe_misclass": sum(u["maybe_misclass"] for _n, u in routing_users),
    }
    routing_total["total_turns"] = (
        routing_total["haiku_turns"] + routing_total["sonnet_turns"]
    )
    routing_total["haiku_pct"] = (
        100.0 * routing_total["haiku_turns"] / routing_total["total_turns"]
        if routing_total["total_turns"] else 0.0
    )
    routing_total["net_savings"] = (
        routing_total["haiku_savings"] - routing_total["router_cost"]
    )
    # Web-search offload savings (Haiku sub-agent vs. doing it inline on
    # Sonnet). Shown on the routing/savings tab alongside model-routing.
    web_search_summary = _aggregate_web_search(records)
    routing_haiku_daily, routing_sonnet_daily = _routing_daily(records, day_keys)
    chart_routing_stack = _svg_stacked_bars(
        day_keys,
        [("haiku", routing_haiku_daily), ("sonnet", routing_sonnet_daily)],
    )

    # Per-user behavior signals for the slim by-user table on Overview. Each
    # is keyed by the same username as `users`; missing keys default to
    # zero/"none" in the template via .get().
    user_badges = {}
    user_session_counts = {}
    user_active_days = {}
    user_median_turn = {}
    for name in users.keys():
        own = [r for r in records
               if (r.get("user") or "(anonymous)") == name]
        b = _user_behavior(own, name)
        user_badges[name] = b["badge"]
        user_session_counts[name] = b["sessions"]
        user_active_days[name] = b["active_days"]
        user_median_turn[name] = b["median_turn_cost"]

    # Period-over-period deltas — surfaced on Overview cards (previously only
    # on Trends). The window we compare against is the prior equal-length
    # window relative to (since, until); _compare_periods handles the unset
    # case by defaulting to last-30d-vs-prior-30d.
    curr_p, prev_p, (cs_p, ce_p, ps_p, pe_p) = _compare_periods(
        all_records, since, until, now=now_local)
    overview_delta = {
        "cost":   _delta_pct(curr_p["cost"],   prev_p["cost"]),
        "calls":  _delta_pct(curr_p["calls"],  prev_p["calls"]),
        "tokens": _delta_pct(curr_p["tokens"], prev_p["tokens"]),
        "users":  _delta_pct(curr_p["users"],  prev_p["users"]),
    }
    overview_prev_window = f"{ps_p.date()} → {pe_p.date()}"
    anonymized = _records_are_anonymized(records)

    # Sessions for the Conversations tab (and reusable elsewhere).
    sessions = _aggregate_by_session(records)
    sessions_total_cost = sum(s["cost"] for s in sessions)
    sessions_avg_cost = (sessions_total_cost / len(sessions)) if sessions else 0.0
    sessions_avg_turns = (
        sum(s["turns"] for s in sessions) / len(sessions)
    ) if sessions else 0.0

    return dict(
        log=path,
        record_count=len(records),
        window=window,
        since_raw=since_raw,
        until_raw=until_raw,
        user_raw=user_raw,
        model_raw=model_raw,
        purpose_raw=purpose_raw,
        all_users=all_users,
        all_models=all_models,
        all_purposes=all_purposes,
        errors=errors,
        # overview
        users=sorted(users.items()),
        grand_cost=grand_cost,
        total_calls=total_calls,
        total_in=total_in,
        total_out=total_out,
        cache_hit_pct=cache_hit_pct,
        by_day=by_day,
        by_model=by_model,
        anonymized=anonymized,
        user_badges=user_badges,
        user_session_counts=user_session_counts,
        user_active_days=user_active_days,
        user_median_turn=user_median_turn,
        delta=overview_delta,
        curr=curr_p,
        prev=prev_p,
        prev_window=overview_prev_window,
        tier_rows=tier_rows,
        # conversations
        sessions=sessions,
        session_count=len(sessions),
        sessions_total_cost=sessions_total_cost,
        sessions_avg_cost=sessions_avg_cost,
        sessions_avg_turns=sessions_avg_turns,
        # overview view toggle
        view=view,
        user_count=user_count,
        card_cost=card_cost,
        card_calls=card_calls,
        card_tokens=card_tokens,
        # cache efficacy
        cache_users=cache_users,
        cache_with=cache_with,
        cache_without=cache_without,
        cache_savings=cache_savings,
        cache_savings_pct=cache_savings_pct,
        cache_reuse=cache_reuse,
        flagged=flagged,
        # activity
        purpose_rows=purpose_rows,
        stop_rows=stop_rows,
        stop_total=stop_total,
        hours=hours,
        hour_max=hour_max,
        lat_n=lat_n,
        lat_avg=lat_avg,
        lat_p50=lat_p50,
        lat_p90=lat_p90,
        lat_p99=lat_p99,
        # chart strings + supporting series
        day_keys=day_keys,
        daily_cost=daily_cost,
        daily_calls=daily_calls,
        daily_active=daily_active,
        daily_savings=daily_savings,
        chart_daily_cost=chart_daily_cost,
        chart_daily_calls=chart_daily_calls,
        chart_model_stack=chart_model_stack,
        chart_purpose_stack=chart_purpose_stack,
        chart_cache_savings=chart_cache_savings,
        chart_latency_day=chart_latency_day,
        chart_hours=chart_hours,
        chart_model_donut=chart_model_donut,
        daily_rows=daily_rows,
        chart_daily_cost_smoothed=chart_daily_cost_smoothed,
        chart_heatmap=chart_heatmap,
        user_sparklines=user_sparklines,
        model_sparklines=model_sparklines,
        # tools
        by_tool=by_tool,
        chart_tool_stack=chart_tool_stack,
        chart_tool_donut=chart_tool_donut,
        tool_sparklines=tool_sparklines,
        tool_total_cost=tool_total_cost,
        tool_total_calls=tool_total_calls,
        tool_top_name=tool_top_name,
        tool_top_cost=tool_top_cost,
        # agents
        agents=agents,
        agent_total_cost=agent_total_cost,
        agent_total_calls=agent_total_calls,
        agent_total_tool_calls=agent_total_tool_calls,
        agent_total_runs=agent_total_runs,
        agent_cost_share=agent_cost_share,
        agent_top_name=agent_top_name,
        agent_top_cost=agent_top_cost,
        chart_agent_stack=chart_agent_stack,
        chart_agent_donut=chart_agent_donut,
        agent_sparklines=agent_sparklines,
        # model routing
        routing_users=routing_users,
        routing_total=routing_total,
        chart_routing_stack=chart_routing_stack,
        web_search_summary=web_search_summary,
        # records carried for Trends-tab post-processing
        _records=records,
        _all_records=all_records,
        _since=since,
        _until=until,
        _now=now_local,
        # tz state — surfaced into the page chrome (filter form, print header,
        # tab-nav qs) so the user's choice survives navigation and re-renders.
        tz_raw=tz_display,
        tz_resolved=tz_raw,
        tz_auto=tz_auto,
        tz_label=tz_label,
        tz_options=_COMMON_TIMEZONES,
    )


def _accounts_context() -> dict:
    """Template context for the Accounts tab — active accounts plus the
    soft-deleted ones annotated with their purge countdown."""
    backend = _auth_backend()
    pending = _accounts.list_pending(backend, grace_days=_GRACE_DAYS)
    active = backend.list_users()
    # Per-user usage-budget snapshot (read-only; never writes the store) so the
    # admin sees each account's remaining daily allowance alongside its tier.
    store = _quota_store()
    usage = {}
    for u in active:
        cap = _config.tier_daily_cap(u.tier)
        ceiling = cap * _config.USAGE_BANK_DAYS
        usage[u.username] = _quota.make_status(
            store.read(u.username, cap, ceiling), cap, ceiling
        )
    return dict(
        active_users=active,
        usage=usage,
        tiers=list(_config.USAGE_TIERS.keys()),
        pending=pending,
        expired_count=sum(1 for p in pending if p.expired),
        grace_days=_GRACE_DAYS,
        # In billing mode Stripe owns api_access for paying users, so the raw
        # grant/revoke toggle is a footgun (the next webhook overrides it).
        # Instead we expose a durable "full access" comp toggle. See the
        # Accounts fragment + docs/billing.md.
        billing_mode=(os.environ.get("AIME_ACCESS_MODE", "keys").strip().lower()
                      == "billing"),
    )


def _keys_context() -> dict:
    """Template context for the Keys tab."""
    return dict(keys=_auth_backend().list_access_keys())


def _billing_context() -> dict:
    """Template context for the Billing tab — the access mode, per-tier
    allowances, and (in billing mode) each user's Stripe subscription status as
    last recorded by the webhook. Read-only: Stripe is the system of record."""
    access_mode = os.environ.get("AIME_ACCESS_MODE", "keys").strip().lower()
    subscribers = []
    if access_mode == "billing":
        subscribers = [
            u for u in _auth_backend().list_users() if u.stripe_customer_id
        ]
    return dict(
        access_mode=access_mode,
        billing_mode=(access_mode == "billing"),
        tiers=_config.USAGE_TIERS,
        bank_days=_config.USAGE_BANK_DAYS,
        subscribers=subscribers,
    )


def _system_context() -> dict:
    """Template context for the System tab.

    Counts + sizes only. Topic / event / conversation files are never opened —
    just directory entries and ``os.path.getsize``. The user's data stays
    opaque to the admin dashboard, by design.
    """
    backend = _auth_backend()
    active = backend.list_users()
    deleted = backend.list_deleted_users()
    keys = backend.list_access_keys()

    db_dir = _config.DATABASE_DIR
    users_root = os.path.join(db_dir, "users")

    per_user = []
    for u in active:
        ud = os.path.join(users_root, str(u.id))
        per_user.append({
            "id": u.id,
            "username": u.username,
            "size": _dir_size(ud),
            "size_h": _format_bytes(_dir_size(ud)),
            "topics": _count_files(os.path.join(ud, "topics"), ".md"),
            "conversations": _count_files(os.path.join(ud, "conversations"), ".json"),
            "exists": os.path.isdir(ud),
        })
    # Largest user first — that's the one most likely to need attention if
    # disk pressure ever shows up.
    per_user.sort(key=lambda r: r["size"], reverse=True)

    per_user_size = sum(u["size"] for u in per_user)

    log_path = _log_path()
    try:
        log_size = os.path.getsize(log_path)
    except OSError:
        log_size = 0

    return dict(
        n_active=len(active),
        n_with_send_access=sum(1 for u in active if u.api_access),
        n_deleted=len(deleted),
        n_keys_total=len(keys),
        n_keys_redeemed=sum(1 for k in keys if k.redeemed),
        n_keys_unredeemed=sum(1 for k in keys if not k.redeemed),
        per_user=per_user,
        per_user_size_h=_format_bytes(per_user_size),
        per_user_topics_total=sum(u["topics"] for u in per_user),
        per_user_conversations_total=sum(u["conversations"] for u in per_user),
        db_dir=db_dir,
        db_dir_size_h=_format_bytes(_dir_size(db_dir)),
        log_size_h=_format_bytes(log_size),
    )


def _security_context(tz_raw: str) -> dict:
    """Template context for the Security tab.

    Resolves the dashboard time zone so the audit log's unix timestamps render
    in the same zone as the rest of the UI, then asks the auth backend for the
    rollups + the newest events.
    """
    backend = _auth_backend()
    zone, tz_label = _resolve_zone(tz_raw)
    counts_24h = backend.auth_event_summary(24 * 60 * 60)
    counts_1h = backend.auth_event_summary(60 * 60)
    top_ips_raw = backend.distinct_event_ips(24 * 60 * 60, limit=10)
    events = backend.recent_auth_events(limit=200)

    def _fmt(ts: int) -> str:
        dt = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)
        if zone is not None:
            dt = dt.astimezone(zone)
        return dt.strftime("%Y-%m-%d %H:%M:%S")

    top_ips = [
        {"ip": r["ip"], "count": r["count"], "last_ts_h": _fmt(r["last_ts"])}
        for r in top_ips_raw
    ]
    events_h = [
        {**e, "ts_h": _fmt(e["ts"])} for e in events
    ]
    return dict(
        counts_24h=counts_24h,
        total_1h=sum(counts_1h.values()),
        top_ips=top_ips,
        events=events_h,
        retention_days=_auth._AUTH_EVENT_TTL // (24 * 60 * 60),
        tz_label=tz_label,
    )


def _trends_context(compute_ctx):
    """Compute period-over-period figures and the anomaly / top-N tables.

    Folds the chart and KPI extras on top of the regular ``_compute`` context —
    the Trends tab shares filters with Overview, so we reuse the same record
    set and aggregations to avoid loading the log twice.
    """
    records = compute_ctx.pop("_records")
    all_records = compute_ctx.pop("_all_records")
    since = compute_ctx.pop("_since")
    until = compute_ctx.pop("_until")
    now = compute_ctx.pop("_now", None)
    day_keys = compute_ctx["day_keys"]
    daily_cost = compute_ctx["daily_cost"]
    daily_active = compute_ctx["daily_active"]

    curr, prev, (cs, ce, ps, pe) = _compare_periods(
        all_records, since, until, now=now)
    delta = {
        "cost":   _delta_pct(curr["cost"],   prev["cost"]),
        "calls":  _delta_pct(curr["calls"],  prev["calls"]),
        "tokens": _delta_pct(curr["tokens"], prev["tokens"]),
        "users":  _delta_pct(curr["users"],  prev["users"]),
    }
    prev_window = f"{ps.date()} → {pe.date()}"

    anomalies = _anomaly_days(daily_cost, day_keys)
    top_calls = _top_calls(records, n=15)

    # Top users by cost across the visible window.
    users_agg = _report.aggregate(records)
    top_users = sorted(users_agg.items(), key=lambda kv: kv[1]["cost"],
                       reverse=True)[:10]
    grand_cost = sum(u["cost"] for u in users_agg.values())

    chart_active_users = _svg_line_chart(
        day_keys, [("active users", daily_active)],
        y_label="users",
        colors=["#c8860a"],
    )

    compute_ctx.update(
        curr=curr,
        prev=prev,
        delta=delta,
        prev_window=prev_window,
        anomalies=anomalies,
        top_calls=top_calls,
        top_users=top_users,
        grand_cost=grand_cost,
        chart_active_users=chart_active_users,
    )
    return compute_ctx


def _users_context(compute_ctx, dormant_days=14):
    """Build the Users-tab template context on top of an Overview-style
    ``_compute`` result. Shares filter args so what you see on Users matches
    what you see on Overview for the same date / model / purpose pick."""
    all_records = compute_ctx.pop("_all_records")
    since = compute_ctx.pop("_since")
    until = compute_ctx.pop("_until")
    now = compute_ctx.pop("_now", None)
    compute_ctx.pop("_records", None)

    classification = _classify_users(all_records, since=since, until=until,
                                     dormant_days=dormant_days, now=now)
    counts = {
        "active": sum(1 for r in classification["rows"] if r["window_calls"] > 0),
        "new": len(classification["new"]),
        "dormant": len(classification["dormant"]),
        "heavy": sum(1 for r in classification["rows"] if r["heavy"]),
    }
    pattern_counts = {
        "daily":      sum(1 for r in classification["rows"] if r["pattern"] == "daily"),
        "most-days":  sum(1 for r in classification["rows"] if r["pattern"] == "most-days"),
        "occasional": sum(1 for r in classification["rows"] if r["pattern"] == "occasional"),
        "once":       sum(1 for r in classification["rows"] if r["pattern"] == "once"),
    }
    cs, ce = classification["window"]
    cohort_window = f"{cs.date()} → {ce.date()}"
    chart_active_users = _svg_line_chart(
        compute_ctx["day_keys"],
        [("active users", compute_ctx["daily_active"])],
        y_label="users", colors=["#c8860a"],
    )
    compute_ctx.update(
        classification=classification,
        counts=counts,
        pattern_counts=pattern_counts,
        cohort_window=cohort_window,
        dormant_days=dormant_days,
        chart_active_users=chart_active_users,
    )
    return compute_ctx


def _user_context(username: str, tz_raw: str = ""):
    """All-time drill-down for a single user."""
    path = _log_path()
    all_records = []
    if os.path.exists(path):
        all_records = list(_report.load_records(path, None, None, None))
    zone, _tz_label = _resolve_zone(tz_raw)
    if zone is not None:
        _shift_records_to_zone(all_records, zone)
    records = [r for r in all_records
               if (r.get("user") or "(anonymous)") == username]

    api_records = [r for r in records if r.get("kind") == "api"]
    users_agg = _report.aggregate(records)
    u = users_agg.get(username, {
        "api_calls": 0, "input": 0, "output": 0, "cost": 0.0,
        "cache_r": 0, "cache_w_5m": 0, "cache_w_1h": 0,
        "web_searches": 0, "stt_calls": 0, "audio_seconds": 0.0, "compute_ms": 0,
    })

    cache_denom = u["input"] + u["cache_r"]
    cache_hit_pct = (100.0 * u["cache_r"] / cache_denom) if cache_denom else 0.0

    lats = sorted(float(r["duration_ms"]) for r in api_records
                  if r.get("duration_ms") is not None)
    lat_p50 = _percentile(lats, 50) if lats else None

    day_keys = _day_keys_in_range(api_records)
    daily_cost = _aggregate_cost_per_day(api_records, day_keys)
    chart_daily_cost = _svg_line_chart(
        day_keys, [(username, daily_cost)],
        y_label="USD", money=True,
    )

    by_model = sorted(_aggregate_by_model(api_records).items(),
                      key=lambda kv: kv[1]["cost"], reverse=True)
    model_slices = [(name, m["cost"], _color_for(i))
                    for i, (name, m) in enumerate(by_model[:8])]
    chart_model_donut = _svg_donut(model_slices)

    by_purpose = sorted(_aggregate_purpose(api_records).items(),
                        key=lambda kv: kv[1]["cost"], reverse=True)
    purpose_slices = [(name, p["cost"], _color_for(i))
                      for i, (name, p) in enumerate(by_purpose[:8])]
    chart_purpose_donut = _svg_donut(purpose_slices)

    behavior = _user_behavior(records, username)
    user_sessions_rows = _user_sessions(records, username)
    weekday_grid = _user_weekday_hour(records)
    weekday_labels = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    hour_labels = [f"{h:02d}" for h in range(24)]
    chart_user_heatmap = _svg_heatmap(
        weekday_grid, row_labels=weekday_labels, col_labels=hour_labels,
        width=520, height=180,
    )

    return dict(
        username=username,
        record_count=len(records),
        has_data=bool(api_records),
        u=u,
        cache_hit_pct=cache_hit_pct,
        lat_p50=lat_p50,
        chart_daily_cost=chart_daily_cost,
        chart_model_donut=chart_model_donut,
        chart_purpose_donut=chart_purpose_donut,
        behavior=behavior,
        user_sessions=user_sessions_rows,
        chart_user_heatmap=chart_user_heatmap,
    )


def _tab(args) -> str:
    t = args.get("tab")
    return t if t in ("overview", "cache", "activity", "tools", "agents",
                      "trends", "users", "conversations", "routing", "accounts",
                      "keys", "billing", "system", "security",
                      "feedback", "errors") else "users"


def _render_fragment(ctx, tab) -> str:
    template = {
        "cache": _FRAGMENT_CACHE,
        "activity": _FRAGMENT_ACTIVITY,
        "tools": _FRAGMENT_TOOLS,
        "agents": _FRAGMENT_AGENTS,
        "trends": _FRAGMENT_TRENDS,
        "users": _FRAGMENT_USERS,
        "conversations": _FRAGMENT_CONVERSATIONS,
        "routing": _FRAGMENT_ROUTING,
        "accounts": _FRAGMENT_ACCOUNTS,
        "keys": _FRAGMENT_KEYS,
        "billing": _FRAGMENT_BILLING,
        "system": _FRAGMENT_SYSTEM,
        "security": _FRAGMENT_SECURITY,
        "feedback": _FRAGMENT_FEEDBACK,
        "errors": _FRAGMENT_ERRORS,
        "overview": _FRAGMENT_OVERVIEW,
    }.get(tab, _FRAGMENT_USERS)
    return render_template_string(template, **ctx)


# ---------------------------------------------------------------------------
# Routes — authentication
# ---------------------------------------------------------------------------


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("admin"):
        return redirect(url_for("index"))
    error = ""
    if request.method == "POST":
        if not _login_limiter.hit(request.remote_addr or "unknown"):
            error = "Too many attempts. Wait a few minutes and try again."
        elif secrets.compare_digest(request.form.get("password", ""),
                                    _ADMIN_PASSWORD):
            # Fresh session id on login — no fixation, and a new CSRF token.
            session.clear()
            session["admin"] = True
            return redirect(url_for("index"))
        else:
            error = "Incorrect password."
    return render_template_string(_LOGIN_PAGE, error=error)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# Routes — pages
# ---------------------------------------------------------------------------


@app.route("/")
@admin_required
def index():
    tab = _tab(request.args)
    csrf = _csrf_token()
    flashes = session.pop("flash", [])
    flash_keys = session.pop("flash_keys", [])

    empty_page_ctx = dict(since_raw="", until_raw="", user_raw="",
                          model_raw="", purpose_raw="",
                          tz_raw="", tz_label="UTC",
                          tz_options=_COMMON_TIMEZONES,
                          all_users=[], all_models=[], all_purposes=[])

    if tab == "accounts":
        ctx = _accounts_context()
        ctx["csrf"] = csrf
        auto = 0
        page_ctx = empty_page_ctx
    elif tab == "keys":
        ctx = _keys_context()
        ctx["csrf"] = csrf
        ctx["flash_keys"] = flash_keys
        auto = 0
        page_ctx = empty_page_ctx
    elif tab == "billing":
        ctx = _billing_context()
        auto = 0
        page_ctx = empty_page_ctx
    elif tab == "system":
        ctx = _system_context()
        auto = 0
        page_ctx = empty_page_ctx
    elif tab == "security":
        # Carry the time-zone query arg so the audit log renders in the same
        # zone as every other tab. The Security tab does not poll, so no
        # auto-refresh is wired up.
        ctx = _security_context(request.args.get("tz", ""))
        auto = 0
        page_ctx = {**empty_page_ctx, "tz_label": ctx["tz_label"]}
    elif tab == "feedback":
        ctx = _feedback_context(request.args)
        ctx["csrf"] = csrf
        auto = 0
        page_ctx = empty_page_ctx
    elif tab == "errors":
        ctx = _errors_context(request.args)
        ctx["csrf"] = csrf
        auto = 0
        page_ctx = empty_page_ctx
    else:
        ctx = _compute(request.args)
        auto = _refresh_seconds(request.args)
        page_ctx = {k: ctx[k] for k in
                    ("since_raw", "until_raw", "user_raw",
                     "model_raw", "purpose_raw",
                     "tz_raw", "tz_label", "tz_options",
                     "all_users", "all_models", "all_purposes")}
        # Overview view toggle: preserve filters, flip `view`.
        if tab == "overview":
            keep_view = {k: request.args.get(k)
                         for k in ("since", "until", "user", "model",
                                   "purpose", "auto", "tz", "tz_auto")
                         if request.args.get(k)}
            ctx["qs_view_total"] = urlencode({**keep_view, "tab": "overview", "view": "total"})
            ctx["qs_view_avg"] = urlencode({**keep_view, "tab": "overview", "view": "avg"})
        if tab == "trends":
            ctx = _trends_context(ctx)
        elif tab == "users":
            ctx = _users_context(ctx)
        else:
            # Drop transient keys carried for the Trends post-process so they
            # don't leak into other fragment renders.
            for k in ("_records", "_all_records", "_since", "_until", "_now"):
                ctx.pop(k, None)

    fragment = _render_fragment(ctx, tab)

    # Tab links carry the active usage filter (since/until/user/model/purpose/auto).
    # `view` is overview-only and intentionally NOT carried so switching tabs
    # doesn't preserve a stale toggle state for tabs that don't have one.
    keep = {k: request.args.get(k)
            for k in ("since", "until", "user", "model", "purpose", "auto",
                      "tz", "tz_auto")
            if request.args.get(k)}
    qs = {t: urlencode({**keep, "tab": t})
          for t in ("overview", "cache", "activity", "tools", "agents", "trends",
                    "users", "conversations", "routing", "accounts", "keys",
                    "billing", "system", "security", "feedback", "errors")}

    # Unresolved-ticket count for the nav badge — shown on every tab. Guarded so
    # a feedback-store hiccup never takes down the whole dashboard.
    try:
        feedback_open = _feedback_store().counts()["unresolved"]
    except Exception:
        feedback_open = 0
    # Likewise for unresolved (new + seen) captured errors.
    try:
        errors_open = _error_store().counts()["unresolved"]
    except Exception:
        errors_open = 0

    return render_template_string(
        _PAGE, fragment=fragment, tab=tab, auto=auto,
        auto_label=_AUTO_LABELS.get(auto, "second"),
        csrf=csrf, flashes=flashes, feedback_open=feedback_open,
        errors_open=errors_open,
        qs_overview=qs["overview"], qs_cache=qs["cache"],
        qs_activity=qs["activity"], qs_tools=qs["tools"],
        qs_agents=qs["agents"],
        qs_trends=qs["trends"], qs_users=qs["users"],
        qs_conversations=qs["conversations"],
        qs_routing=qs["routing"],
        qs_accounts=qs["accounts"], qs_keys=qs["keys"], qs_billing=qs["billing"],
        qs_system=qs["system"], qs_security=qs["security"],
        qs_feedback=qs["feedback"], qs_errors=qs["errors"], **page_ctx,
    )


def _feedback_context(args) -> dict:
    """Tickets for the Feedback tab, optionally filtered by status, plus the
    per-status counts for the summary chips."""
    store = _feedback_store()
    status = args.get("status", "")
    if status not in _feedback.STATUSES:
        status = ""
    tickets = store.list(status or None)
    return {
        "tickets": tickets,
        "counts": store.counts(),
        "status_filter": status,
        "statuses": _feedback.STATUSES,
        "qs_all": urlencode({"tab": "feedback"}),
        "qs_open": urlencode({"tab": "feedback", "status": "open"}),
        "qs_in_progress": urlencode({"tab": "feedback", "status": "in_progress"}),
        "qs_resolved": urlencode({"tab": "feedback", "status": "resolved"}),
    }


def _errors_context(args) -> dict:
    """Captured errors for the Errors tab, optionally filtered by status, plus
    the per-status counts for the summary chips."""
    store = _error_store()
    status = args.get("status", "")
    if status not in _errors.STATUSES:
        status = ""
    rows = store.list(status or None)
    return {
        "errors": rows,
        "counts": store.counts(),
        "status_filter": status,
        "statuses": _errors.STATUSES,
        "qs_all": urlencode({"tab": "errors"}),
        "qs_new": urlencode({"tab": "errors", "status": "new"}),
        "qs_seen": urlencode({"tab": "errors", "status": "seen"}),
        "qs_resolved": urlencode({"tab": "errors", "status": "resolved"}),
    }


@app.route("/fragment")
@admin_required
def fragment():
    """The data region only — polled on the refresh interval by the usage
    tabs. Admin tabs do not poll, so only overview/cache/activity/trends
    reach here."""
    tab = _tab(request.args)
    ctx = _compute(request.args)
    if tab == "trends":
        ctx = _trends_context(ctx)
    elif tab == "users":
        ctx = _users_context(ctx)
    else:
        for k in ("_records", "_all_records", "_since", "_until", "_now"):
            ctx.pop(k, None)
    return _render_fragment(ctx, tab)


@app.route("/user/<path:username>")
@admin_required
def user_drilldown(username):
    """Per-user drill-down — KPI cards, daily-cost trend, model & purpose
    mix, recent activity. Always shows all-time figures; the visible Overview
    filter set is intentionally not applied so the page is a coherent
    snapshot of the user, not a slice of one."""
    csrf = _csrf_token()
    flashes = session.pop("flash", [])
    tz_raw = request.args.get("tz", "")
    tz_auto = request.args.get("tz_auto") == "1"
    ctx = _user_context(username, tz_raw=tz_raw)
    fragment = render_template_string(_FRAGMENT_USER, **ctx)

    # No filter form on the user page; pass the empty page-ctx + qs so the
    # tab nav still renders.
    _, tz_label = _resolve_zone(tz_raw)
    empty_page_ctx = dict(since_raw="", until_raw="", user_raw="",
                          model_raw="", purpose_raw="",
                          tz_raw="auto" if tz_auto else tz_raw,
                          tz_label=tz_label,
                          tz_options=_COMMON_TIMEZONES,
                          all_users=[], all_models=[], all_purposes=[])
    keep_tz = {k: request.args.get(k) for k in ("tz", "tz_auto")
               if request.args.get(k)}
    qs = {t: urlencode({**keep_tz, "tab": t})
          for t in ("overview", "cache", "activity", "tools", "agents", "trends",
                    "users", "conversations", "routing", "accounts", "keys",
                    "billing", "system", "security")}
    return render_template_string(
        _PAGE, fragment=fragment, tab="user", auto=0,
        auto_label="second",
        csrf=csrf, flashes=flashes,
        qs_overview=qs["overview"], qs_cache=qs["cache"],
        qs_activity=qs["activity"], qs_tools=qs["tools"],
        qs_agents=qs["agents"],
        qs_trends=qs["trends"], qs_users=qs["users"],
        qs_conversations=qs["conversations"],
        qs_routing=qs["routing"],
        qs_accounts=qs["accounts"], qs_keys=qs["keys"], qs_billing=qs["billing"],
        qs_system=qs["system"], qs_security=qs["security"], **empty_page_ctx,
    )


@app.route("/session/<path:session_id>")
@admin_required
def session_drilldown(session_id):
    """Timeline + breakdowns for one session_id. Linked from the
    Conversations tab and from a user's drill-down."""
    csrf = _csrf_token()
    flashes = session.pop("flash", [])
    tz_raw = request.args.get("tz", "")
    tz_auto = request.args.get("tz_auto") == "1"

    path = _log_path()
    all_records = []
    if os.path.exists(path):
        all_records = list(_report.load_records(path, None, None, None))
    zone, tz_label = _resolve_zone(tz_raw)
    if zone is not None:
        _shift_records_to_zone(all_records, zone)

    header, timeline, by_model, by_tool, by_purpose, total_cost = (
        _session_detail(all_records, session_id)
    )

    # Donut breakdowns reuse the standard SVG helper. Donut slices want a
    # stable colour per model/purpose; iteration order in the underlying
    # dicts is insertion order, which is acceptable here.
    model_slices = [(name, cost, _color_for(i))
                    for i, (name, cost) in enumerate(
                        sorted(by_model.items(), key=lambda kv: kv[1], reverse=True))]
    purpose_slices = [(name, cost, _color_for(i))
                      for i, (name, cost) in enumerate(
                          sorted(by_purpose.items(), key=lambda kv: kv[1], reverse=True))]
    chart_model_donut = _svg_donut(model_slices[:8])
    chart_purpose_donut = _svg_donut(purpose_slices[:8])

    tool_call_counts: dict = {}
    for r in all_records:
        if r.get("session_id") == session_id and r.get("kind") == "tool":
            n = r.get("tool_name") or "(unknown)"
            tool_call_counts[n] = tool_call_counts.get(n, 0) + 1

    fragment = render_template_string(
        _FRAGMENT_SESSION,
        header=header,
        timeline=timeline,
        by_tool=sorted(by_tool.items(), key=lambda kv: kv[1], reverse=True),
        tool_call_counts=tool_call_counts,
        total_cost=total_cost,
        chart_model_donut=chart_model_donut,
        chart_purpose_donut=chart_purpose_donut,
    )

    empty_page_ctx = dict(since_raw="", until_raw="", user_raw="",
                          model_raw="", purpose_raw="",
                          tz_raw="auto" if tz_auto else tz_raw,
                          tz_label=tz_label,
                          tz_options=_COMMON_TIMEZONES,
                          all_users=[], all_models=[], all_purposes=[])
    keep_tz = {k: request.args.get(k) for k in ("tz", "tz_auto")
               if request.args.get(k)}
    qs = {t: urlencode({**keep_tz, "tab": t})
          for t in ("overview", "cache", "activity", "tools", "agents", "trends",
                    "users", "conversations", "routing", "accounts", "keys",
                    "billing", "system", "security")}
    return render_template_string(
        _PAGE, fragment=fragment, tab="session", auto=0,
        auto_label="second",
        csrf=csrf, flashes=flashes,
        qs_overview=qs["overview"], qs_cache=qs["cache"],
        qs_activity=qs["activity"], qs_tools=qs["tools"],
        qs_agents=qs["agents"],
        qs_trends=qs["trends"], qs_users=qs["users"],
        qs_conversations=qs["conversations"],
        qs_routing=qs["routing"],
        qs_accounts=qs["accounts"], qs_keys=qs["keys"], qs_billing=qs["billing"],
        qs_system=qs["system"], qs_security=qs["security"], **empty_page_ctx,
    )


# ---------------------------------------------------------------------------
# Routes — account administration
# ---------------------------------------------------------------------------


@app.route("/accounts/access", methods=["POST"])
@admin_post
def account_access():
    username = (request.form.get("username") or "").strip()
    grant = request.form.get("grant") == "1"
    if _auth_backend().set_api_access_by_username(username, grant):
        verb = "Granted" if grant else "Revoked"
        _flash("ok", f"{verb} send access for {username!r}.")
    else:
        _flash("bad", f"No such user: {username!r}.")
    return redirect(url_for("index", tab="accounts"))


@app.route("/accounts/comp", methods=["POST"])
@admin_post
def account_comp():
    """Grant or remove complimentary "always-allow" access. Sets comp_access +
    api_access together so the user gains/loses send access; in billing mode this
    also makes the Stripe webhook stop reconciling them (see aime.billing +
    docs/billing.md). Surfaced in *both* modes: billing as "full access", keys as
    "always-allow + reset". On a grant it also refills the user's usage budget to
    100% (QuotaStore.reset_full), so the toggle doubles as a per-user reset — the
    daily budget still applies afterward, matching billing's comp (see
    docs/usage-limits.md)."""
    username = (request.form.get("username") or "").strip()
    grant = request.form.get("grant") == "1"
    billing = (os.environ.get("AIME_ACCESS_MODE", "keys").strip().lower()
               == "billing")
    if _auth_backend().set_comp_access_by_username(username, grant):
        if grant:
            # Refill the bank to 100% as part of the same click. Look the user up
            # for their tier (which sets the ceiling); a missing record just skips
            # the refill rather than failing the access grant.
            rec = _auth_backend().lookup_by_username(username)
            if rec is not None:
                cap = _config.tier_daily_cap(rec.tier)
                _quota_store().reset_full(username, cap * _config.USAGE_BANK_DAYS)
            extra = (" They can use Aime without a subscription, and billing "
                     "won't revoke them.") if billing else ""
            _flash("ok", f"Granted always-allow access to {username!r} and reset "
                         f"their usage to 100%.{extra}")
        else:
            _flash("ok", f"Removed always-allow access for {username!r}; send "
                         f"access is now off.")
    else:
        _flash("bad", f"No such user: {username!r}.")
    return redirect(url_for("index", tab="accounts"))


@app.route("/accounts/set-tier", methods=["POST"])
@admin_post
def account_set_tier():
    username = (request.form.get("username") or "").strip()
    tier = (request.form.get("tier") or "").strip().lower()
    if tier not in _config.USAGE_TIERS:
        _flash("bad", f"Unknown tier: {tier!r}.")
    elif _auth_backend().set_tier_by_username(username, tier):
        _flash("ok", f"Set {username!r} to the {tier!r} tier "
                     f"(${_config.USAGE_TIERS[tier]:.2f}/day).")
    else:
        _flash("bad", f"No such user: {username!r}.")
    return redirect(url_for("index", tab="accounts"))


@app.route("/accounts/delete", methods=["POST"])
@admin_post
def account_delete():
    username = (request.form.get("username") or "").strip()
    if _auth_backend().soft_delete_by_username(username):
        _flash("ok", f"Soft-deleted {username!r}. It can be restored within "
                     f"the {_GRACE_DAYS}-day grace period.")
    else:
        _flash("bad", f"No such active user: {username!r}.")
    return redirect(url_for("index", tab="accounts"))


@app.route("/accounts/restore", methods=["POST"])
@admin_post
def account_restore():
    username = (request.form.get("username") or "").strip()
    if _auth_backend().restore_by_username(username):
        _flash("ok", f"Restored {username!r}; the account is active again. "
                     f"Send access is off — grant it explicitly if needed.")
    else:
        _flash("bad", f"No soft-deleted user named {username!r}.")
    return redirect(url_for("index", tab="accounts"))


@app.route("/accounts/purge", methods=["POST"])
@admin_post
def account_purge():
    results = _accounts.purge_expired(_auth_backend(), grace_days=_GRACE_DAYS)
    if results:
        names = ", ".join(repr(p.user.username) for p, _ in results)
        _flash("ok", f"Purged {len(results)} account(s): {names}. "
                     f"A final backup was written for each.")
    else:
        _flash("warn", "Nothing to purge — no account is past the grace period.")
    return redirect(url_for("index", tab="accounts"))


@app.route("/accounts/revoke-all", methods=["POST"])
@admin_post
def account_revoke_all():
    n = _auth_backend().revoke_all_access()
    _flash("ok", f"Revoked send access for {n} user(s).")
    return redirect(url_for("index", tab="accounts"))


@app.route("/accounts/trial", methods=["POST"])
@admin_post
def account_trial():
    """Toggle one account's free-trial eligibility. used=1 → a (re)subscribe is
    charged immediately, no fresh trial; used=0 → eligible again. The web
    equivalent of `access_keys.py deny-trial / allow-trial <user>`."""
    username = (request.form.get("username") or "").strip()
    used = request.form.get("used") == "1"
    if _auth_backend().set_trial_used_by_username(username, used):
        _flash("ok", (f"{username!r} will be charged immediately on subscribe "
                      f"(no free trial)." if used else
                      f"{username!r} is eligible for the free trial again."))
    else:
        _flash("bad", f"No such user: {username!r}.")
    return redirect(url_for("index", tab="accounts"))


@app.route("/accounts/deny-trial-all", methods=["POST"])
@admin_post
def account_deny_trial_all():
    """Billing-cutover bulk: deny a fresh free trial to every existing account
    (new signups still get one). Pairs with revoke-all. Mirrors
    `access_keys.py deny-trial --all`."""
    n = _auth_backend().mark_all_trial_used()
    _flash("ok", f"Marked {n} account(s) trial-used — no fresh trial on "
                 f"subscribe (new signups still get one).")
    return redirect(url_for("index", tab="accounts"))


# ---------------------------------------------------------------------------
# Routes — invite-key administration
# ---------------------------------------------------------------------------


@app.route("/keys/gen", methods=["POST"])
@admin_post
def keys_gen():
    try:
        count = int(request.form.get("count", "1"))
    except (TypeError, ValueError):
        count = 1
    count = max(1, min(count, 50))
    note = (request.form.get("note") or "").strip()
    backend = _auth_backend()
    # Raw keys are unrecoverable after this — stash them for a one-shot display
    # on the redirect target, then they are gone.
    session["flash_keys"] = [backend.generate_access_key(note) for _ in range(count)]
    _flash("ok", f"Generated {count} invite key(s). Copy them now — they are "
                 f"not shown again.")
    return redirect(url_for("index", tab="keys"))


@app.route("/keys/revoke", methods=["POST"])
@admin_post
def keys_revoke():
    key_hash = (request.form.get("key_hash") or "").strip()
    if _auth_backend().revoke_access_key_by_hash(key_hash):
        _flash("ok", "Invite key revoked; it can no longer be redeemed.")
    else:
        _flash("bad", "Key not found or already redeemed.")
    return redirect(url_for("index", tab="keys"))


# ---------------------------------------------------------------------------
# Routes — feedback ticket triage
# ---------------------------------------------------------------------------


def _feedback_redirect():
    """Back to the Feedback tab, preserving the active status filter."""
    status_filter = (request.form.get("status_filter") or "").strip()
    args = {"tab": "feedback"}
    if status_filter in _feedback.STATUSES:
        args["status"] = status_filter
    return redirect(url_for("index", **args))


@app.route("/feedback/status", methods=["POST"])
@admin_post
def feedback_status():
    try:
        ticket_id = int(request.form.get("id", ""))
    except (TypeError, ValueError):
        _flash("bad", "Bad ticket id.")
        return _feedback_redirect()
    status = (request.form.get("status") or "").strip()
    if _feedback_store().set_status(ticket_id, status):
        _flash("ok", f"Ticket #{ticket_id} moved to "
                     f"{status.replace('_', ' ')!r}.")
    else:
        _flash("bad", f"Couldn't update ticket #{ticket_id} "
                      f"(unknown status or missing ticket).")
    return _feedback_redirect()


@app.route("/feedback/note", methods=["POST"])
@admin_post
def feedback_note():
    try:
        ticket_id = int(request.form.get("id", ""))
    except (TypeError, ValueError):
        _flash("bad", "Bad ticket id.")
        return _feedback_redirect()
    note = request.form.get("note") or ""
    if _feedback_store().set_note(ticket_id, note):
        _flash("ok", f"Saved note on ticket #{ticket_id}.")
    else:
        _flash("bad", f"No such ticket: #{ticket_id}.")
    return _feedback_redirect()


def _errors_redirect():
    """Back to the Errors tab, preserving the active status filter."""
    status_filter = (request.form.get("status_filter") or "").strip()
    args = {"tab": "errors"}
    if status_filter in _errors.STATUSES:
        args["status"] = status_filter
    return redirect(url_for("index", **args))


@app.route("/errors/status", methods=["POST"])
@admin_post
def errors_status():
    try:
        error_id = int(request.form.get("id", ""))
    except (TypeError, ValueError):
        _flash("bad", "Bad error id.")
        return _errors_redirect()
    status = (request.form.get("status") or "").strip()
    if _error_store().set_status(error_id, status):
        _flash("ok", f"Error #{error_id} moved to {status!r}.")
    else:
        _flash("bad", f"Couldn't update error #{error_id} "
                      f"(unknown status or missing row).")
    return _errors_redirect()


@app.route("/errors/note", methods=["POST"])
@admin_post
def errors_note():
    try:
        error_id = int(request.form.get("id", ""))
    except (TypeError, ValueError):
        _flash("bad", "Bad error id.")
        return _errors_redirect()
    note = request.form.get("note") or ""
    if _error_store().set_note(error_id, note):
        _flash("ok", f"Saved note on error #{error_id}.")
    else:
        _flash("bad", f"No such error: #{error_id}.")
    return _errors_redirect()


def main() -> None:
    if not _ADMIN_PASSWORD:
        sys.stderr.write(
            "Error: AIME_ADMIN_PASSWORD is not set.\n"
            "The admin dashboard manages accounts and invite keys, so it "
            "requires a password.\nSet AIME_ADMIN_PASSWORD in the environment "
            "(or .env) and start it again.\n"
        )
        raise SystemExit(1)
    print(f"Aime admin dashboard → http://{_HOST}:{_PORT}/")
    app.run(host=_HOST, port=_PORT, debug=False)


if __name__ == "__main__":
    main()
