"""Opt-in usage statistics.

Records two kinds of resource consumption to an append-only JSONL log:

  * **api**  — Anthropic API token usage (input / output / cache tokens),
               i.e. how much a user costs in billable model calls.
  * **stt**  — local speech-to-text compute (audio seconds transcribed and
               wall-clock time spent), i.e. how much local CPU a user burns.

The log lives at ``<DATABASE_DIR>/usage/usage.jsonl`` — one JSON object per
line, each stamped with an ISO-8601 ``ts``. JSONL is deliberately boring: it
appends cheaply, survives a crash mid-write (only the last line is ever at
risk), and is trivially queryable over any time range by filtering on ``ts``.
See ``scripts/usage_report.py`` for an aggregating viewer.

Two environment variables gate behaviour, both **off by default** so a fresh
install collects nothing until the operator opts in:

  AIME_USAGE_STATS=1       enable collection at all (0 → every record() call
                           is a cheap no-op)
  AIME_USAGE_LINK_USERS=1  tag each record with the username (0 → the `user`
                           field is null, so stats stay aggregate/anonymous)

This module is intentionally self-contained and failure-tolerant: a recording
error must never disrupt a chat turn or a transcription, so every public
function swallows its own exceptions.
"""

import os
import json
import threading
import datetime

import aime.config as _config


_LOG_NAME = "usage.jsonl"
# Serializes appends across the turn thread, background Haiku threads, and
# concurrent STT requests so two writers can't interleave a line.
_write_lock = threading.Lock()


def _enabled() -> bool:
    """True when usage collection is switched on (AIME_USAGE_STATS=1)."""
    try:
        return bool(int(os.environ.get("AIME_USAGE_STATS", "0")))
    except ValueError:
        return False


def _link_users() -> bool:
    """True when records may be tagged with a username (AIME_USAGE_LINK_USERS=1)."""
    try:
        return bool(int(os.environ.get("AIME_USAGE_LINK_USERS", "0")))
    except ValueError:
        return False


def _log_path() -> str:
    return os.path.join(_config.DATABASE_DIR, "usage", _LOG_NAME)


def _append(record: dict) -> None:
    """Append one record as a JSON line. Best-effort: any IO error is
    swallowed so a stats failure can never break the feature being measured."""
    try:
        path = _log_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        line = json.dumps(record, separators=(",", ":")) + "\n"
        with _write_lock:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
    except (OSError, TypeError, ValueError):
        pass


def _base_record(kind: str, user: str | None) -> dict:
    return {
        "ts": datetime.datetime.now().isoformat(timespec="seconds"),
        "kind": kind,
        # Username linkage is a separate opt-in; when off, records are kept
        # but anonymized so period totals still work without identifying who.
        "user": user if (user and _link_users()) else None,
    }


def _attr(obj, name):
    """Read `name` off `obj`, whether it's a dict or an SDK/pydantic object."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def record_api(
    user: str | None,
    model: str,
    usage,
    purpose: str = "turn",
    *,
    session_id: str | None = None,
    stop_reason: str | None = None,
    duration_ms: float | None = None,
    routed_decision: str | None = None,
    source: str = "interactive",
) -> None:
    """Record one Anthropic API call's token usage.

    `usage` is the SDK usage object (or any object/dict exposing the
    ``*_tokens`` attributes); missing fields default to 0. `purpose` tags what
    the call was for — "turn" for a user-facing assistant turn, or "title" /
    "compaction" for the cheap background Haiku calls.

    The remaining keyword fields make a record answer questions beyond raw
    cost:

      * ``session_id``  — ties the call to a conversation, so a report can
                          surface which sessions are expensive. Identifying,
                          so it is gated behind ``AIME_USAGE_LINK_USERS`` just
                          like ``user`` — null when linkage is off.
      * ``stop_reason`` — "end_turn" / "tool_use" / "max_tokens" / ..., so you
                          can see how often turns are truncated or tool-driven.
      * ``duration_ms`` — wall-clock latency of the API call.
      * ``routed_decision`` — "haiku" or "sonnet" when the model-routing
                              layer picked the model for this turn. Lets the
                              dashboard compute Haiku-vs-Sonnet savings by
                              re-pricing the same token counts at the other
                              pole's rates. None for unrouted calls (compaction,
                              title, route classifier itself).
      * ``source``      — "interactive" for a call made on behalf of a live
                          chat session, or "agent" for one made by a headless
                          background-agent run. Lets the dashboard split out
                          what a user's autonomous agents cost from what they
                          cost in live chat. Deliberately a low-cardinality flag
                          (not an agent name — that would explode across every
                          user's agents); agent cost is attributed to the
                          owning ``user``. Non-identifying, so recorded whenever
                          stats are on (unlike user/session_id).

    Cache *writes* are recorded split by TTL — the Anthropic usage object
    carries a nested ``cache_creation`` breakdown, and a 1-hour cache write is
    billed at 2x base input price versus 1.25x for a 5-minute write. Recording
    them separately is what makes the cost figure in usage_report.py exact for
    billing rather than an approximation off the lumped total.

    Server-side ``web_search`` requests are billed by Anthropic as a flat
    per-request charge *independent of tokens* ($10 / 1000). The count lives in
    the usage object's nested ``server_tool_use`` block; recording it is what
    keeps the cost figure from silently omitting web search entirely.
    """
    if not _enabled():
        return

    def _int(val) -> int:
        try:
            return int(val) if val is not None else 0
        except (TypeError, ValueError):
            return 0

    # Per-TTL cache-write breakdown lives in the nested `cache_creation`
    # object. Older API responses may omit it; fall back to attributing the
    # lumped `cache_creation_input_tokens` total to the 5-minute bucket (its
    # cheaper rate — never over-bills the user if the breakdown is missing).
    cache_creation = _attr(usage, "cache_creation")
    cc_5m = _int(_attr(cache_creation, "ephemeral_5m_input_tokens"))
    cc_1h = _int(_attr(cache_creation, "ephemeral_1h_input_tokens"))
    cc_total = _int(_attr(usage, "cache_creation_input_tokens"))
    if cache_creation is None and cc_total:
        cc_5m = cc_total

    # Server-side web_search request count — billed flat, not by token.
    server_tool_use = _attr(usage, "server_tool_use")
    web_search_requests = _int(_attr(server_tool_use, "web_search_requests"))

    rec = _base_record("api", user)
    rec.update({
        "model": model or "",
        "purpose": purpose,
        # `session_id` is the on-disk name of the user's encrypted conversation
        # file. Recording it lets a report group records by conversation, but
        # it is also identifying — it can cluster records and be cross-checked
        # against the conversations directory. So it rides the SAME opt-in as
        # the username (`AIME_USAGE_LINK_USERS`): when linkage is off, this
        # stays null and the log remains aggregate/anonymous. The fields below
        # it (stop_reason, duration, token counts, web search count) are
        # non-identifying aggregate metadata and need only AIME_USAGE_STATS.
        "session_id": session_id if (session_id and _link_users()) else None,
        "stop_reason": stop_reason or None,
        "duration_ms": round(float(duration_ms), 1) if duration_ms is not None else None,
        "input_tokens": _int(_attr(usage, "input_tokens")),
        "output_tokens": _int(_attr(usage, "output_tokens")),
        "cache_read_tokens": _int(_attr(usage, "cache_read_input_tokens")),
        # Total kept for convenience; the two TTL splits are what get priced.
        "cache_creation_tokens": cc_total,
        "cache_creation_5m_tokens": cc_5m,
        "cache_creation_1h_tokens": cc_1h,
        "web_search_requests": web_search_requests,
        "routed_decision": routed_decision or None,
        # Who drove this call: a live chat ("interactive") or a background
        # agent run ("agent"). Non-identifying, so always stamped.
        "source": source or "interactive",
    })
    _append(rec)


def record_tool_use(
    user: str | None,
    tool_name: str,
    tool_kind: str,
    *,
    model: str | None = None,
    result_bytes: int = 0,
    web_search_requests: int = 0,
    session_id: str | None = None,
    source: str = "interactive",
) -> None:
    """Record one tool invocation by the agent.

    Emitted once per tool_use block: client tools record at the point the UI
    returns its tool_result (so `result_bytes` reflects what actually gets
    re-injected as fresh input on the next turn); server tools (web_search)
    record inline when their server-side result lands.

    `tool_kind` is "client" (locally executed, gateway-backed) or "server"
    (Anthropic-side, e.g. web_search). For server tools, `web_search_requests`
    is the count Anthropic billed for that block — used to render exact
    flat-rate cost rather than estimating from bytes.

    `model` is the model id of the turn that emitted the tool_use, recorded
    so the dashboard can price the result's downstream input-token cost at
    that turn's rate (rather than guessing a default).

    ``source`` carries the same meaning as on ``record_api`` — "interactive"
    vs a background-agent run — so a user's agent tool cost can be separated
    from their live-chat tool cost.
    """
    if not _enabled():
        return
    rec = _base_record("tool", user)
    rec.update({
        "tool_name": tool_name or "(unknown)",
        "tool_kind": tool_kind or "client",
        "model": model or "",
        "result_bytes": int(result_bytes or 0),
        "web_search_requests": int(web_search_requests or 0),
        "session_id": session_id if (session_id and _link_users()) else None,
        "source": source or "interactive",
    })
    _append(rec)


def record_stt(
    user: str | None,
    model: str,
    audio_seconds: float,
    compute_ms: float,
) -> None:
    """Record one local speech-to-text transcription.

    `audio_seconds` is the duration of the supplied audio; `compute_ms` is the
    wall-clock time the local Whisper model spent transcribing it.
    """
    if not _enabled():
        return
    rec = _base_record("stt", user)
    rec.update({
        "model": model or "",
        "audio_seconds": round(float(audio_seconds), 3),
        "compute_ms": round(float(compute_ms), 1),
    })
    _append(rec)
