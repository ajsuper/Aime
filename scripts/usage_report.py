#!/usr/bin/env python3
"""Aggregate and view Aime usage statistics over a time period.

Reads the append-only JSONL log written by `aime.usage`
(<database>/usage/usage.jsonl) and prints a per-user breakdown of API token
cost and local speech-to-text compute.

Examples:
    # Everything on record
    ./scripts/usage_report.py

    # Just the last 7 days
    ./scripts/usage_report.py --since 2026-05-13

    # A specific window, one user
    ./scripts/usage_report.py --since 2026-05-01 --until 2026-05-15 --user alice

    # Machine-readable
    ./scripts/usage_report.py --json

Dates accept YYYY-MM-DD or a full ISO-8601 timestamp. --until is inclusive of
the whole day when only a date is given.
"""

import os
import sys
import json
import argparse
import datetime


# Anthropic base list prices in USD per million tokens — base input and
# output only. Edit to match current pricing. Unknown models fall back to
# "default". Cache rates are NOT listed separately: they are fixed multiples
# of the base input price, applied in _api_cost():
#
#   cache read           = 0.10x base input
#   cache write, 5m TTL  = 1.25x base input
#   cache write, 1h TTL  = 2.00x base input
#
# Recording cache writes split by TTL (see aime/usage.py) is what lets this
# script bill the 1h writes at 2x and the 5m writes at 1.25x exactly, rather
# than guessing an average off a lumped total.
CACHE_READ_MULT = 0.10
CACHE_WRITE_5M_MULT = 1.25
CACHE_WRITE_1H_MULT = 2.00

# Server-side web_search is billed as a flat per-request charge, independent
# of token usage: $10 per 1,000 requests. Recorded as `web_search_requests`
# on each api record by aime.usage.
WEB_SEARCH_COST_PER_REQUEST = 10.00 / 1000.0

PRICES = {
    "default":                  {"in": 3.00, "out": 15.00},
    "claude-sonnet-4-6":        {"in": 3.00, "out": 15.00},
    "claude-haiku-4-5":         {"in": 1.00, "out":  5.00},
    "claude-haiku-4-5-20251001":{"in": 1.00, "out":  5.00},
}


def _default_log_path() -> str:
    db = os.environ.get(
        "AIME_DATABASE_DIR",
        os.path.join(os.environ.get("HOME", ""), ".local/share/aime-assistant/database"),
    )
    return os.path.join(db, "usage", "usage.jsonl")


def _parse_bound(text: str, *, end: bool) -> datetime.datetime:
    """Parse a --since/--until argument. A bare date as --until covers the
    whole day (rolls to 23:59:59)."""
    try:
        if len(text) == 10:  # YYYY-MM-DD
            d = datetime.date.fromisoformat(text)
            if end:
                return datetime.datetime.combine(d, datetime.time(23, 59, 59))
            return datetime.datetime.combine(d, datetime.time.min)
        return datetime.datetime.fromisoformat(text)
    except ValueError:
        sys.exit(f"error: could not parse date/time: {text!r}")


def _price_for(model: str) -> dict:
    """Look up base prices for a model.

    The API stamps records with a *dated* model id (e.g.
    "claude-sonnet-4-6-20260101"), which won't match a bare PRICES key. Try an
    exact hit first, then the longest PRICES key that is a prefix of the model
    id, and only then fall back to "default"."""
    if model in PRICES:
        return PRICES[model]
    candidates = [k for k in PRICES if k != "default" and model.startswith(k)]
    if candidates:
        return PRICES[max(candidates, key=len)]
    return PRICES["default"]


def _cache_write_tokens(rec: dict) -> tuple[int, int]:
    """Return (5m, 1h) cache-write token counts for a record.

    New records carry the per-TTL split directly. Records written before the
    split was added only have the lumped `cache_creation_tokens` — attribute
    those to the 5-minute bucket (the cheaper rate, so an old record can never
    be over-billed)."""
    cc_5m = rec.get("cache_creation_5m_tokens")
    cc_1h = rec.get("cache_creation_1h_tokens")
    if cc_5m is None and cc_1h is None:
        return rec.get("cache_creation_tokens", 0), 0
    return cc_5m or 0, cc_1h or 0


def _api_cost(rec: dict) -> float:
    p = _price_for(rec.get("model", ""))
    cc_5m, cc_1h = _cache_write_tokens(rec)
    token_cost = (
        rec.get("input_tokens", 0)        * p["in"]
        + rec.get("output_tokens", 0)     * p["out"]
        + rec.get("cache_read_tokens", 0) * p["in"] * CACHE_READ_MULT
        + cc_5m                           * p["in"] * CACHE_WRITE_5M_MULT
        + cc_1h                           * p["in"] * CACHE_WRITE_1H_MULT
    ) / 1_000_000.0
    # Web search is billed flat per request, on top of token cost.
    return token_cost + rec.get("web_search_requests", 0) * WEB_SEARCH_COST_PER_REQUEST


def load_records(path, since, until, user_filter):
    """Yield records within the time window, optionally filtered by user."""
    try:
        f = open(path, encoding="utf-8")
    except OSError as e:
        sys.exit(f"error: cannot read usage log {path}: {e}")
    with f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                ts = datetime.datetime.fromisoformat(rec["ts"])
            except (ValueError, KeyError):
                continue  # skip a corrupt/partial line
            if since and ts < since:
                continue
            if until and ts > until:
                continue
            if user_filter is not None and (rec.get("user") or "(anonymous)") != user_filter:
                continue
            yield rec


def aggregate(records):
    """Fold records into per-user totals."""
    users = {}
    for rec in records:
        name = rec.get("user") or "(anonymous)"
        u = users.setdefault(name, {
            "api_calls": 0, "input": 0, "output": 0,
            "cache_w_5m": 0, "cache_w_1h": 0, "cache_r": 0, "cost": 0.0,
            "web_searches": 0,
            "stt_calls": 0, "audio_seconds": 0.0, "compute_ms": 0.0,
        })
        if rec.get("kind") == "api":
            cc_5m, cc_1h = _cache_write_tokens(rec)
            u["api_calls"]    += 1
            u["input"]        += rec.get("input_tokens", 0)
            u["output"]       += rec.get("output_tokens", 0)
            u["cache_w_5m"]   += cc_5m
            u["cache_w_1h"]   += cc_1h
            u["cache_r"]      += rec.get("cache_read_tokens", 0)
            u["web_searches"] += rec.get("web_search_requests", 0)
            u["cost"]         += _api_cost(rec)
        elif rec.get("kind") == "stt":
            u["stt_calls"]     += 1
            u["audio_seconds"] += rec.get("audio_seconds", 0.0)
            u["compute_ms"]    += rec.get("compute_ms", 0.0)
    return users


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--log", default=_default_log_path(),
                    help="path to usage.jsonl (default: <database>/usage/usage.jsonl)")
    ap.add_argument("--since", help="start of window (YYYY-MM-DD or ISO timestamp)")
    ap.add_argument("--until", help="end of window (YYYY-MM-DD or ISO timestamp)")
    ap.add_argument("--user", help="restrict to a single username")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    args = ap.parse_args()

    since = _parse_bound(args.since, end=False) if args.since else None
    until = _parse_bound(args.until, end=True) if args.until else None

    records = list(load_records(args.log, since, until, args.user))
    users = aggregate(records)

    if args.json:
        print(json.dumps(users, indent=2, sort_keys=True))
        return

    window = "all time"
    if since or until:
        window = f"{args.since or 'start'} → {args.until or 'now'}"
    print(f"\nAime usage report — {window}")
    print(f"log: {args.log}")
    print(f"records: {len(records)}\n")

    if not users:
        print("(no usage recorded in this window)\n")
        return

    grand_cost = 0.0
    for name in sorted(users):
        u = users[name]
        grand_cost += u["cost"]
        print(f"  {name}")
        print(f"    API:  {u['api_calls']:>5} calls  "
              f"in={u['input']:,}  out={u['output']:,}  "
              f"cache wr(5m/1h)={u['cache_w_5m']:,}/{u['cache_w_1h']:,}  "
              f"cache rd={u['cache_r']:,}  "
              f"web search={u['web_searches']:,}  "
              f"~${u['cost']:.4f}")
        print(f"    STT:  {u['stt_calls']:>5} calls  "
              f"audio={u['audio_seconds']:.1f}s  "
              f"compute={u['compute_ms'] / 1000.0:.1f}s")
    print(f"\n  estimated total API cost: ~${grand_cost:.4f}\n")


if __name__ == "__main__":
    main()
