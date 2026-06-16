"""Anthropic cost model — the single source of truth for pricing a call.

Originally this lived inside ``scripts/usage_report.py`` (and was imported into
the admin dashboard from there). It was lifted into the core ``aime`` package so
that the live cost-control path (``aime.quota``) can price a turn without
reaching into ``scripts/`` — and so the report, the dashboard, and the quota
meter can never disagree on a dollar figure.

``scripts/usage_report.py`` re-exports everything here, so the CLI and the
dashboard (which both reference ``usage_report._api_cost`` etc.) keep working
unchanged.

Prices are Anthropic base list prices in USD per *million* tokens — base input
and output only. Cache rates are fixed multiples of the base input price
(applied in :func:`api_cost`):

  cache read           = 0.10x base input
  cache write, 5m TTL  = 1.25x base input
  cache write, 1h TTL  = 2.00x base input

Recording cache writes split by TTL (see ``aime/usage.py``) is what lets the
cost figure bill 1h writes at 2x and 5m writes at 1.25x exactly, rather than
guessing an average off a lumped total.
"""

CACHE_READ_MULT = 0.10
CACHE_WRITE_5M_MULT = 1.25
CACHE_WRITE_1H_MULT = 2.00

# Server-side web_search is billed as a flat per-request charge, independent of
# token usage: $10 per 1,000 requests. Recorded as `web_search_requests` on each
# api record by aime.usage.
WEB_SEARCH_COST_PER_REQUEST = 10.00 / 1000.0

PRICES = {
    "default":                  {"in": 3.00, "out": 15.00},
    "claude-sonnet-4-6":        {"in": 3.00, "out": 15.00},
    "claude-haiku-4-5":         {"in": 1.00, "out":  5.00},
    "claude-haiku-4-5-20251001":{"in": 1.00, "out":  5.00},
}


def price_for(model: str) -> dict:
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


def cache_write_tokens(rec: dict) -> tuple[int, int]:
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


def api_cost(rec: dict) -> float:
    """USD cost of one api usage record (the same record shape ``aime.usage``
    writes). Token cost at the model's base rates plus the flat per-request
    web-search charge."""
    p = price_for(rec.get("model", ""))
    cc_5m, cc_1h = cache_write_tokens(rec)
    token_cost = (
        rec.get("input_tokens", 0)        * p["in"]
        + rec.get("output_tokens", 0)     * p["out"]
        + rec.get("cache_read_tokens", 0) * p["in"] * CACHE_READ_MULT
        + cc_5m                           * p["in"] * CACHE_WRITE_5M_MULT
        + cc_1h                           * p["in"] * CACHE_WRITE_1H_MULT
    ) / 1_000_000.0
    # Web search is billed flat per request, on top of token cost.
    return token_cost + rec.get("web_search_requests", 0) * WEB_SEARCH_COST_PER_REQUEST


def _attr(obj, name):
    """Read `name` off `obj`, whether it's a dict or an SDK/pydantic object."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _int(val) -> int:
    try:
        return int(val) if val is not None else 0
    except (TypeError, ValueError):
        return 0


def record_from_usage(model: str, usage) -> dict:
    """Build a priceable record dict from a raw Anthropic SDK usage object (or a
    dict exposing the same ``*_tokens`` fields). This is the single extraction
    path shared by the usage log (:func:`aime.usage.record_api`) and the live
    cost-control debit (:mod:`aime.quota`), so the two can never price the same
    call differently.

    Cache writes are split by TTL: the nested ``cache_creation`` block carries
    the 5m/1h breakdown; older responses only have the lumped
    ``cache_creation_input_tokens``, which is attributed to the cheaper 5m bucket
    (never over-bills). The flat-rate ``web_search_requests`` count lives in the
    nested ``server_tool_use`` block.
    """
    cache_creation = _attr(usage, "cache_creation")
    cc_5m = _int(_attr(cache_creation, "ephemeral_5m_input_tokens"))
    cc_1h = _int(_attr(cache_creation, "ephemeral_1h_input_tokens"))
    cc_total = _int(_attr(usage, "cache_creation_input_tokens"))
    if cache_creation is None and cc_total:
        cc_5m = cc_total
    server_tool_use = _attr(usage, "server_tool_use")
    web_search_requests = _int(_attr(server_tool_use, "web_search_requests"))
    return {
        "model": model or "",
        "input_tokens": _int(_attr(usage, "input_tokens")),
        "output_tokens": _int(_attr(usage, "output_tokens")),
        "cache_read_tokens": _int(_attr(usage, "cache_read_input_tokens")),
        "cache_creation_tokens": cc_total,
        "cache_creation_5m_tokens": cc_5m,
        "cache_creation_1h_tokens": cc_1h,
        "web_search_requests": web_search_requests,
    }


def cost_from_usage(model: str, usage) -> float:
    """Convenience: price a raw SDK usage object in one call."""
    return api_cost(record_from_usage(model, usage))


# Back-compat aliases. The report/dashboard historically referenced these with
# a leading underscore (module-private style); keep them so those call sites and
# `from aime.pricing import *`-style re-exports resolve unchanged.
_price_for = price_for
_cache_write_tokens = cache_write_tokens
_api_cost = api_cost
