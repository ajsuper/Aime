"""Local-only web dashboard for Aime usage statistics.

A small Flask app that loads the append-only usage log written by
`aime.usage` (<database>/usage/usage.jsonl) and presents it as a readable
page instead of a terminal table.

Two tabs:
  * Overview        — per-user / per-day / per-model token cost.
  * Cache Efficacy  — whether prompt caching is actually saving money:
                      reuse factors, hypothetical no-cache cost, and a
                      warning when message spacing outlives the 5m cache.

The figures refresh on a selectable interval (1s / 30s / 5m, or off). The
refresh re-fetches only the data region (an HTML fragment) and swaps it in
place — the filter form, its focus, and the page scroll position are left
untouched.

Deliberately **loopback-only**: the usage log can carry usernames and
per-conversation ids (when AIME_USAGE_LINK_USERS is on), so this server binds
127.0.0.1 and refuses to advertise itself on the network. It is a read-only
viewer — it never writes the log.

Run from the project's `src/` directory:

    python -m frontends.usage_dashboard

then open http://127.0.0.1:5050/.
"""

import os
import sys
import datetime
from urllib.parse import urlencode

from flask import Flask, render_template_string, request

# Allow `python -m frontends.usage_dashboard` from src/ to find the aime
# package and the scripts/ directory.
_SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO = os.path.dirname(_SRC)
sys.path.insert(0, _SRC)
sys.path.insert(0, os.path.join(_REPO, "scripts"))

# Reuse the exact cost model, aggregation, and log-path resolution from the
# CLI report so the web view and `usage_report.py` can never disagree on a
# dollar figure. Importing this module is cheap — it pulls in no Anthropic SDK
# or `aime` package machinery.
import usage_report as _report  # noqa: E402

app = Flask(__name__)

# Loopback only — see module docstring. The port is overridable but the host
# is not, on purpose.
_HOST = "127.0.0.1"
_PORT = int(os.environ.get("AIME_USAGE_DASHBOARD_PORT", "5050"))

# Allowed auto-refresh intervals, in seconds. 0 = off. Anything else is
# rejected back to the default so a hand-edited query string can't wedge the
# page into a 1ms reload loop.
_REFRESH_CHOICES = (0, 1, 30, 300)
_REFRESH_DEFAULT = 1

# 5-minute cache TTL, in seconds. Median request spacing above this means a
# 5m-TTL cache write tends to expire before it is ever read back.
_CACHE_5M_TTL = 300


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

  <div class="cards">
    <div class="card accent-green" title="Total estimated USD cost of every API call in this window: input, output, cache, and web-search charges combined. An estimate from list prices — not your actual invoice.">
      <div class="num good">${{ "%.4f"|format(grand_cost) }}</div>
      <div class="lbl">estimated cost</div></div>
    <div class="card accent-blue" title="Number of requests sent to the Anthropic Messages API in this window.">
      <div class="num blue">{{ "{:,}".format(total_calls) }}</div>
      <div class="lbl">API calls</div></div>
    <div class="card accent-purple" title="Fresh (uncached) input tokens plus output tokens. Excludes cache read/write tokens — see 'cache read share'.">
      <div class="num purple">{{ "{:,}".format(total_in + total_out) }}</div>
      <div class="lbl">tokens (in+out)</div></div>
    <div class="card {{ 'accent-green' if cache_hit_pct >= 70 else 'accent-amber' if cache_hit_pct >= 40 else 'accent-red' }}"
      title="Share of read-side prompt tokens served from cache (cache reads) rather than billed as fresh input. Higher is cheaper. Green at 70%+, amber 40-70%, red below 40%.">
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
        <th title="Estimated USD cost for this user: tokens, cache, and web search combined.">Est. cost</th>
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
    <div class="card accent-blue" title="Actual prompt-token cost with caching on: fresh input at 1x, cache reads at 0.10x, 5m writes at 1.25x, 1h writes at 2x base input. Output and web-search cost are excluded — they are identical with or without caching.">
      <div class="num blue">${{ "%.4f"|format(cache_with) }}</div>
      <div class="lbl">prompt cost, caching on</div></div>
    <div class="card accent-purple" title="Hypothetical prompt-token cost with caching off: every cache read and cache write token re-billed as plain input at 1x base rate.">
      <div class="num purple">${{ "%.4f"|format(cache_without) }}</div>
      <div class="lbl">prompt cost, no caching</div></div>
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
        <th title="Actual prompt-token cost for this user with caching on.">Cost (cache)</th>
        <th title="Hypothetical prompt-token cost for this user if caching were off.">Cost (no cache)</th>
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


_PAGE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Aime usage</title>
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
  <h1 title="Read-only viewer for Aime's usage log (usage.jsonl). Loopback-only — never exposed on the network.">Aime usage</h1>

  <nav class="tabs">
    <a href="?{{ qs_overview }}" class="{{ 'active' if tab == 'overview' else '' }}"
      title="Per-user, per-day and per-model token usage and cost.">Overview</a>
    <a href="?{{ qs_cache }}" class="{{ 'active' if tab == 'cache' else '' }}"
      title="Whether prompt caching is actually saving money — reuse factors, hypothetical no-cache cost, and 5-minute-TTL warnings.">Cache Efficacy</a>
  </nav>

  <form class="filter" method="get">
    <input type="hidden" name="tab" value="{{ tab }}">
    <label title="Only include records on or after this date. Accepts YYYY-MM-DD or a full ISO-8601 timestamp. Leave blank for no lower bound.">Since
      <input type="text" name="since" value="{{ since_raw }}" placeholder="YYYY-MM-DD">
    </label>
    <label title="Only include records on or before this date. A bare YYYY-MM-DD covers the whole day (through 23:59:59). Leave blank for no upper bound.">Until
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

    by_day = sorted(_aggregate_by_day(records).items(), reverse=True)
    by_model = sorted(_aggregate_by_model(records).items(),
                      key=lambda kv: kv[1]["cost"], reverse=True)

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
        # cache efficacy
        cache_users=cache_users,
        cache_with=cache_with,
        cache_without=cache_without,
        cache_savings=cache_savings,
        cache_savings_pct=cache_savings_pct,
        cache_reuse=cache_reuse,
        flagged=flagged,
    )


def _tab(args) -> str:
    return "cache" if args.get("tab") == "cache" else "overview"


def _render_fragment(ctx, tab) -> str:
    template = _FRAGMENT_CACHE if tab == "cache" else _FRAGMENT_OVERVIEW
    return render_template_string(template, **ctx)


@app.route("/")
def index():
    ctx = _compute(request.args)
    tab = _tab(request.args)
    auto = _refresh_seconds(request.args)
    fragment = _render_fragment(ctx, tab)

    # Tab links carry the active filter (since/until/user/auto) across.
    keep = {k: request.args.get(k) for k in ("since", "until", "user", "auto")
            if request.args.get(k)}
    qs_overview = urlencode({**keep, "tab": "overview"})
    qs_cache = urlencode({**keep, "tab": "cache"})

    return render_template_string(
        _PAGE, fragment=fragment, tab=tab, auto=auto,
        auto_label=_AUTO_LABELS.get(auto, "second"),
        qs_overview=qs_overview, qs_cache=qs_cache, **ctx,
    )


@app.route("/fragment")
def fragment():
    """The data region only — polled on the refresh interval by the open page."""
    ctx = _compute(request.args)
    return _render_fragment(ctx, _tab(request.args))


def main() -> None:
    print(f"Aime usage dashboard → http://{_HOST}:{_PORT}/  (loopback only)")
    app.run(host=_HOST, port=_PORT, debug=False)


if __name__ == "__main__":
    main()
