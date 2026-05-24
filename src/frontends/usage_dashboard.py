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
_REFRESH_DEFAULT = 1

# 5-minute cache TTL, in seconds. Median request spacing above this means a
# 5m-TTL cache write tends to expire before it is ever read back.
_CACHE_5M_TTL = 300

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
    <span title="Filesystem path of the usage.jsonl log being read. Resolved from AIME_DATABASE_DIR, identical to the CLI usage report.">log: {{ log }}</span><br>
    <span title="Number of log records matching the current filters.">{{ record_count }} records</span>
    &middot;
    <span title="Date range currently in view, set by the Since / Until filters above.">window: {{ window }}</span>
  </div>

  {% for e in errors %}<p class="err">{{ e }}</p>{% endfor %}

  {% if not users %}
    <p class="empty">No usage recorded in this window. Collection is enabled
    with AIME_USAGE_STATS=1.</p>
  {% else %}

  <div class="view-toggle"
    title="Switch the headline cards between window totals and the per-user average for the {{ user_count }} user(s) active in this window. The 'cache read share' card is a ratio and is unaffected.">
    <span class="lbl">Show:</span>
    <a href="?{{ qs_view_total }}" class="{{ 'active' if view == 'total' else '' }}"
      title="Show totals across every user active in this window.">Total</a>
    <a href="?{{ qs_view_avg }}" class="{{ 'active' if view == 'avg' else '' }}"
      title="Divide the cost / call / token figures by the {{ user_count }} user(s) active in this window. The 'cache read share' card is a ratio and is unaffected.">Avg / user</a>
    <span class="note">({{ user_count }} user{{ '' if user_count == 1 else 's' }} active in this window)</span>
  </div>

  <div class="cards">
    <div class="card accent-green"
      title="{% if view == 'avg' %}Per-user average — total estimated USD cost divided by the {{ user_count }} user(s) active in this window.{% else %}Total estimated USD cost of every API call in this window: input + output + cache + web-search charges combined.{% endif %} An estimate from list prices — not your actual invoice. Larger than the Cache Efficacy tab's 'prompt cost' figures, which isolate prompt-side tokens only.">
      <div class="num good">${{ "%.4f"|format(card_cost) }}</div>
      <div class="lbl">{{ 'avg cost / user' if view == 'avg' else 'estimated total cost (all charges)' }}</div></div>
    <div class="card accent-blue"
      title="{% if view == 'avg' %}Average requests sent per active user.{% else %}Number of requests sent to the Anthropic Messages API in this window.{% endif %}">
      <div class="num blue">{{ ('%.1f' % card_calls) if view == 'avg' else '{:,}'.format(card_calls) }}</div>
      <div class="lbl">{{ 'avg API calls / user' if view == 'avg' else 'API calls' }}</div></div>
    <div class="card accent-purple"
      title="{% if view == 'avg' %}Average fresh input + output tokens per active user.{% else %}Fresh (uncached) input tokens plus output tokens.{% endif %} Excludes cache read/write tokens — see 'cache read share'.">
      <div class="num purple">{{ ('%.0f' % card_tokens) if view == 'avg' else '{:,}'.format(card_tokens) }}</div>
      <div class="lbl">{{ 'avg tokens / user' if view == 'avg' else 'tokens (in+out)' }}</div></div>
    <div class="card {{ 'accent-green' if cache_hit_pct >= 70 else 'accent-amber' if cache_hit_pct >= 40 else 'accent-red' }}"
      title="Share of read-side prompt tokens served from cache (cache reads) rather than billed as fresh input. Higher is cheaper. This is a ratio, identical whether you view totals or per-user averages. Green at 70%+, amber 40-70%, red below 40%.">
      <div class="num {{ 'good' if cache_hit_pct >= 70 else 'warn' if cache_hit_pct >= 40 else 'bad' }}">{{ "%.0f"|format(cache_hit_pct) }}%</div>
      <div class="lbl">cache read share</div></div>
  </div>

  <h2 title="One row per user, totalling every API and speech-to-text record in the window.">By user</h2>
  <table>
    <thead>
      <tr>
        <th title="Username the record was logged under. (anonymous) covers records with no username (AIME_USAGE_LINK_USERS=0).">User</th>
        <th title="Requests sent to the Anthropic Messages API.">API calls</th>
        <th title="Fresh, uncached input tokens — billed at the model's base input rate.">Input</th>
        <th title="Tokens generated by the model — billed at the base output rate.">Output</th>
        <th title="Tokens written into the prompt cache, split by time-to-live. 5m write is billed at 1.25x base input, 1h write at 2x.">Cache wr (5m/1h)</th>
        <th title="Tokens read back from the prompt cache — billed at just 0.10x base input.">Cache rd</th>
        <th title="Server-side web_search tool requests — billed flat at $10 per 1,000 requests.">Web search</th>
        <th title="Local speech-to-text transcription calls.">STT calls</th>
        <th title="Total seconds of audio transcribed by speech-to-text.">Audio (s)</th>
        <th title="Wall-clock seconds of local compute spent on speech-to-text.">Compute (s)</th>
        <th title="Estimated total USD cost for this user: input + output + cache reads/writes + web-search charges combined. This is broader than the Cache Efficacy tab's 'Cost (cache)' column, which only counts the prompt side (fresh input + cache reads + cache writes) and excludes output tokens and web-search charges — so that figure will always be smaller.">Est. total cost</th>
      </tr>
    </thead>
    <tbody>
      {% for name, u in users %}
      <tr>
        <td>{{ name }}</td>
        <td>{{ "{:,}".format(u.api_calls) }}</td>
        <td>{{ "{:,}".format(u.input) }}</td>
        <td>{{ "{:,}".format(u.output) }}</td>
        <td>{{ "{:,}".format(u.cache_w_5m) }} / {{ "{:,}".format(u.cache_w_1h) }}</td>
        <td>{{ "{:,}".format(u.cache_r) }}</td>
        <td>{{ "{:,}".format(u.web_searches) }}</td>
        <td>{{ "{:,}".format(u.stt_calls) }}</td>
        <td>{{ "%.1f"|format(u.audio_seconds) }}</td>
        <td>{{ "%.1f"|format(u.compute_ms / 1000.0) }}</td>
        <td class="cost good">${{ "%.4f"|format(u.cost) }}</td>
      </tr>
      {% endfor %}
    </tbody>
    <tfoot>
      <tr>
        <td>Total</td>
        <td colspan="9"></td>
        <td class="cost good">${{ "%.4f"|format(grand_cost) }}</td>
      </tr>
    </tfoot>
  </table>

  <h2 title="API token cost grouped by calendar date (UTC), newest first.">By day</h2>
  <table>
    <thead>
      <tr>
        <th title="Calendar date (UTC) the requests were made.">Date</th>
        <th title="Requests sent to the Anthropic Messages API on this date.">API calls</th>
        <th title="Fresh, uncached input tokens on this date.">Input</th>
        <th title="Tokens generated by the model on this date.">Output</th>
        <th title="Estimated USD cost for this date.">Est. cost</th>
      </tr>
    </thead>
    <tbody>
      {% for day, d in by_day %}
      <tr>
        <td>{{ day }}</td>
        <td>{{ "{:,}".format(d.api_calls) }}</td>
        <td>{{ "{:,}".format(d.input) }}</td>
        <td>{{ "{:,}".format(d.output) }}</td>
        <td class="cost good">${{ "%.4f"|format(d.cost) }}</td>
      </tr>
      {% endfor %}
    </tbody>
  </table>

  <h2 title="API token cost grouped by the model id stamped on each record, most expensive first.">By model</h2>
  <table>
    <thead>
      <tr>
        <th title="Model id the API stamped on the record (e.g. claude-sonnet-4-6). (unknown) means the record carried no model.">Model</th>
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
        <td>{{ "{:,}".format(m.api_calls) }}</td>
        <td>{{ "{:,}".format(m.input) }}</td>
        <td>{{ "{:,}".format(m.output) }}</td>
        <td class="cost good">${{ "%.4f"|format(m.cost) }}</td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
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
    <div class="card accent-blue" title="Actual prompt-side cost with caching on — counting only fresh input (1x), cache reads (0.10x), 5m writes (1.25x), and 1h writes (2x base input). Excludes output tokens and web-search charges, which are identical with or without caching and would only obscure the comparison. This is smaller than Overview's 'Est. total cost' for the same reason.">
      <div class="num blue">${{ "%.4f"|format(cache_with) }}</div>
      <div class="lbl">prompt-side cost, caching on</div></div>
    <div class="card accent-purple" title="Hypothetical prompt-side cost with caching off: every cache read and cache write token re-billed as plain input at the 1x base rate. Output and web-search charges are excluded for the same reason as the 'caching on' card.">
      <div class="num purple">${{ "%.4f"|format(cache_without) }}</div>
      <div class="lbl">prompt-side cost, no caching</div></div>
    <div class="card {{ 'accent-green' if cache_savings >= 0 else 'accent-red' }}"
      title="No-cache cost minus actual cost. Positive (green) means caching saved money; negative (red) means the write premium outran the read discount.">
      <div class="num {{ 'good' if cache_savings >= 0 else 'bad' }}">${{ "%.4f"|format(cache_savings) }}</div>
      <div class="lbl">net savings</div></div>
    <div class="card {{ 'accent-green' if cache_reuse >= 3 else 'accent-amber' if cache_reuse >= 1 else 'accent-red' }}"
      title="Cache-read tokens divided by cache-write tokens — how many times the average cached segment is read back. Green at 3x+, amber 1-3x, red below 1x (writes not recouped).">
      <div class="num {{ 'good' if cache_reuse >= 3 else 'warn' if cache_reuse >= 1 else 'bad' }}">{{ "%.2f"|format(cache_reuse) }}&times;</div>
      <div class="lbl">cache reuse factor</div></div>
  </div>

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
        <th title="Actual prompt-side cost for this user with caching on — counting only fresh input + cache reads + cache writes. Excludes output tokens and web-search charges (those are identical with or without caching), so this is smaller than the Overview tab's 'Est. total cost' column.">Prompt cost (cache)</th>
        <th title="Hypothetical prompt-side cost for this user if caching were off — every cache read/write re-billed as plain input. Also excludes output and web-search charges, for the same apples-to-apples reason.">Prompt cost (no cache)</th>
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

  <h2 title="API calls bucketed by UTC hour-of-day. Tall bars are busy hours. Use this to spot off-hours background traffic, or to size capacity to peak.">Hourly traffic (UTC)</h2>
  <div class="hour-bars" title="One bar per UTC hour 00..23. Bar height is calls in that hour relative to the busiest hour ({{ '{:,}'.format(hour_max) }} calls).">
    {% for h in hours %}
    <div class="bar {{ 'empty' if h == 0 else '' }}"
      style="height: {{ ((100.0 * h / hour_max)|round(1)) if hour_max else 0 }}%"
      title="{{ '%02d:00' % loop.index0 }} UTC — {{ '{:,}'.format(h) }} call{{ '' if h == 1 else 's' }}"></div>
    {% endfor %}
  </div>
  <div class="hour-axis">
    {% for h in hours %}<span>{{ '%02d' % loop.index0 }}</span>{% endfor %}
  </div>

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
        <th title="Grant/revoke send access, or soft-delete the account.">Actions</th>
      </tr>
    </thead>
    <tbody>
      {% for u in active_users %}
      <tr>
        <td>#{{ u.id }}</td>
        <td>{{ u.username }}</td>
        <td class="{{ 'good' if u.api_access else 'bad' }}">{{ 'yes' if u.api_access else 'no' }}</td>
        <td class="actions">
          <form method="post" action="accounts/access">
            <input type="hidden" name="csrf" value="{{ csrf }}">
            <input type="hidden" name="username" value="{{ u.username }}">
            <input type="hidden" name="grant" value="{{ '0' if u.api_access else '1' }}">
            <button type="submit">{{ 'Revoke access' if u.api_access else 'Grant access' }}</button>
          </form>
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
  <form method="post" action="accounts/revoke-all" class="inline-action"
    onsubmit="return confirm('Revoke send access for ALL users? This is the billing-cutover action.')">
    <input type="hidden" name="csrf" value="{{ csrf }}">
    <button type="submit" class="danger">Revoke send access for everyone</button>
    <span class="note">Zeroes api_access for every account (billing cutover).</span>
  </form>
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
    body { font: 15px/1.5 system-ui, sans-serif; margin: 2rem auto; max-width: 1000px; padding: 0 1rem; }
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
    nav.tabs { display: flex; gap: .3rem; border-bottom: 2px solid #8884; margin-bottom: 1rem; }
    nav.tabs a { padding: .45rem .9rem; text-decoration: none; color: #888;
      border: 1px solid transparent; border-bottom: none; border-radius: 6px 6px 0 0; }
    nav.tabs a:hover { background: #8881; }
    nav.tabs a.active { color: inherit; font-weight: 600;
      border-color: #8884; background: #8881; margin-bottom: -2px; }

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
    <form method="post" action="logout" class="logout-form">
      <input type="hidden" name="csrf" value="{{ csrf }}">
      <button type="submit" class="logout">Log out</button>
    </form>
  </div>

  <nav class="tabs">
    <a href="?{{ qs_overview }}" class="{{ 'active' if tab == 'overview' else '' }}"
      title="Per-user, per-day and per-model token usage and cost.">Overview</a>
    <a href="?{{ qs_cache }}" class="{{ 'active' if tab == 'cache' else '' }}"
      title="Whether prompt caching is actually saving money — reuse factors, hypothetical no-cache cost, and 5-minute-TTL warnings.">Cache Efficacy</a>
    <a href="?{{ qs_activity }}" class="{{ 'active' if tab == 'activity' else '' }}"
      title="What the API is being used for — call purpose, stop-reason mix, latency percentiles, and when of day traffic happens.">Activity</a>
    <a href="?{{ qs_accounts }}" class="{{ 'active' if tab == 'accounts' else '' }}"
      title="List, grant/revoke, soft-delete, restore and purge user accounts.">Accounts</a>
    <a href="?{{ qs_keys }}" class="{{ 'active' if tab == 'keys' else '' }}"
      title="Mint and revoke single-use invite keys.">Keys</a>
    <a href="?{{ qs_system }}" class="{{ 'active' if tab == 'system' else '' }}"
      title="Operator-level health: account/key counts, storage per user (sizes and file counts only — never content), usage-log status.">System</a>
  </nav>

  {% for f in flashes %}
  <div class="flash {{ f.level }}">{{ f.msg }}</div>
  {% endfor %}

  {% if tab in ('overview', 'cache', 'activity') %}
  <form class="filter" method="get">
    <input type="hidden" name="tab" value="{{ tab }}">
    <label title="Only include records on or after this date, interpreted in UTC. Accepts YYYY-MM-DD or a full ISO-8601 timestamp. Leave blank for no lower bound.">Since (UTC)
      <input type="text" name="since" value="{{ since_raw }}" placeholder="YYYY-MM-DD">
    </label>
    <label title="Only include records on or before this date, interpreted in UTC. A bare YYYY-MM-DD covers the whole UTC day (through 23:59:59 UTC). Leave blank for no upper bound.">Until (UTC)
      <input type="text" name="until" value="{{ until_raw }}" placeholder="YYYY-MM-DD">
    </label>
    <div class="quick-group" title="Quick presets that fill the Since / Until fields and apply immediately."><span>Quick range</span>
      <span class="quick">
        <button type="button" title="Today only." onclick="quickRange(0)">Today</button>
        <button type="button" title="Today and the previous 6 days." onclick="quickRange(7)">7d</button>
        <button type="button" title="Today and the previous 29 days." onclick="quickRange(30)">30d</button>
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
  <p class="note" title="Dates and timestamps throughout the dashboard — the Since/Until filters, the By day grouping, and log timestamps — are all UTC. The dashboard runs in a container with no timezone configured, so it uses UTC by default.">All dates and times shown are <strong>UTC</strong>.</p>
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


def _compute(args):
    """Build the template context for the current filter query args."""
    path = _log_path()

    since_raw = args.get("since", "").strip()
    until_raw = args.get("until", "").strip()
    user_raw = args.get("user", "").strip()

    since, since_err = _parse_bound(since_raw, end=False)
    until, until_err = _parse_bound(until_raw, end=True)
    errors = [e for e in (since_err, until_err) if e]

    # Load the whole log once so the user dropdown lists everyone, even when
    # the current filter would hide them. A bad date is treated as "no bound".
    all_records = []
    if os.path.exists(path):
        all_records = list(_report.load_records(path, None, None, None))
    all_users = sorted({r.get("user") or "(anonymous)" for r in all_records})

    # Apply the window / user filter in Python against the already-loaded set.
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
        return True

    records = [r for r in all_records if _keep(r)]

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

    return dict(
        log=path,
        record_count=len(records),
        window=window,
        since_raw=since_raw,
        until_raw=until_raw,
        user_raw=user_raw,
        all_users=all_users,
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
    )


def _accounts_context() -> dict:
    """Template context for the Accounts tab — active accounts plus the
    soft-deleted ones annotated with their purge countdown."""
    backend = _auth_backend()
    pending = _accounts.list_pending(backend, grace_days=_GRACE_DAYS)
    return dict(
        active_users=backend.list_users(),
        pending=pending,
        expired_count=sum(1 for p in pending if p.expired),
        grace_days=_GRACE_DAYS,
    )


def _keys_context() -> dict:
    """Template context for the Keys tab."""
    return dict(keys=_auth_backend().list_access_keys())


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


def _tab(args) -> str:
    t = args.get("tab")
    return t if t in ("overview", "cache", "activity", "accounts", "keys", "system") else "overview"


def _render_fragment(ctx, tab) -> str:
    template = {
        "cache": _FRAGMENT_CACHE,
        "activity": _FRAGMENT_ACTIVITY,
        "accounts": _FRAGMENT_ACCOUNTS,
        "keys": _FRAGMENT_KEYS,
        "system": _FRAGMENT_SYSTEM,
    }.get(tab, _FRAGMENT_OVERVIEW)
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

    if tab == "accounts":
        ctx = _accounts_context()
        ctx["csrf"] = csrf
        auto = 0
        page_ctx = dict(since_raw="", until_raw="", user_raw="", all_users=[])
    elif tab == "keys":
        ctx = _keys_context()
        ctx["csrf"] = csrf
        ctx["flash_keys"] = flash_keys
        auto = 0
        page_ctx = dict(since_raw="", until_raw="", user_raw="", all_users=[])
    elif tab == "system":
        ctx = _system_context()
        auto = 0
        page_ctx = dict(since_raw="", until_raw="", user_raw="", all_users=[])
    else:
        ctx = _compute(request.args)
        auto = _refresh_seconds(request.args)
        page_ctx = {k: ctx[k] for k in
                    ("since_raw", "until_raw", "user_raw", "all_users")}
        # Overview view toggle: preserve filters, flip `view`.
        if tab == "overview":
            keep_view = {k: request.args.get(k)
                         for k in ("since", "until", "user", "auto")
                         if request.args.get(k)}
            ctx["qs_view_total"] = urlencode({**keep_view, "tab": "overview", "view": "total"})
            ctx["qs_view_avg"] = urlencode({**keep_view, "tab": "overview", "view": "avg"})

    fragment = _render_fragment(ctx, tab)

    # Tab links carry the active usage filter (since/until/user/auto) across.
    # `view` is overview-only and intentionally NOT carried so switching tabs
    # doesn't preserve a stale toggle state for tabs that don't have one.
    keep = {k: request.args.get(k) for k in ("since", "until", "user", "auto")
            if request.args.get(k)}
    qs = {t: urlencode({**keep, "tab": t})
          for t in ("overview", "cache", "activity", "accounts", "keys", "system")}

    return render_template_string(
        _PAGE, fragment=fragment, tab=tab, auto=auto,
        auto_label=_AUTO_LABELS.get(auto, "second"),
        csrf=csrf, flashes=flashes,
        qs_overview=qs["overview"], qs_cache=qs["cache"],
        qs_activity=qs["activity"], qs_accounts=qs["accounts"],
        qs_keys=qs["keys"], qs_system=qs["system"], **page_ctx,
    )


@app.route("/fragment")
@admin_required
def fragment():
    """The data region only — polled on the refresh interval by the usage
    tabs. Admin tabs do not poll, so only overview/cache reach here."""
    ctx = _compute(request.args)
    return _render_fragment(ctx, _tab(request.args))


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
