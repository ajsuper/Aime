"""Commitment-pattern aggregation over the events store.

The three model-facing tools `GetCommitmentHistory`, `GetPatternSummary`, and
`GetRecentActivity` don't hit a dedicated backend endpoint — they fetch with the
existing `get_events` tool and compute counts/streaks/summaries here in Python.
That keeps the C++ side to just the new columns (which flow through `get_events`
unchanged) and lets the controller intercept these tools the same way it does
`WebSearch`.

Every method returns a compact text digest (not raw JSON) for the same reason
`tool_formatting.format_tool_result_for_model` does: it's what gets fed back into
the model's cached context, so terse-but-complete beats verbose."""

from collections import Counter

from .tool_gateway import ToolGateway
from .services import _events_from, sort_events_by_date

# Status values that mean an instance actually resolved one way or the other —
# the ones streaks and "last completed" reason about (vs. still-scheduled).
_TERMINAL_STATUSES = ("completed", "canceled")
_FAR_FUTURE = "31/12/9999"


def _status_of(ev: dict) -> str:
    """An event's status, defaulting to 'scheduled' for legacy rows that predate
    the column (where it comes back blank)."""
    return (ev.get("status") or "scheduled").strip() or "scheduled"


def _events_desc(events: list[dict]) -> list[dict]:
    """Newest first."""
    return list(reversed(sort_events_by_date(events)))


class CommitmentService:
    """Read-only pattern queries. Fetches via the gateway's `get_events` and
    aggregates in Python."""

    def __init__(self, gateway: ToolGateway):
        self._gw = gateway

    def _fetch(self, *, category: str = "", since_date: str = "") -> list[dict] | str:
        """All events (any archived state), optionally narrowed to a category and
        a `since_date` lower bound. Returns the event list, or an error string the
        caller can forward straight to the model."""
        payload = {"archived": "all", "sort_order": "asc"}
        if category:
            payload["category"] = category
        if since_date:
            payload["filter_by_date"] = True
            payload["start_date"] = since_date
            payload["end_date"] = _FAR_FUTURE
        data = self._gw.call("get_events", **payload)
        if isinstance(data, dict) and "error" in data:
            return f"Error fetching events: {data.get('error')}"
        return _events_from(data)

    # ── GetCommitmentHistory ────────────────────────────────────────────────
    def commitment_history(
        self, commitment_id: str, since_date: str = "", limit: int = 0
    ) -> str:
        commitment_id = (commitment_id or "").strip()
        if not commitment_id:
            return "Error: commitment_id is required."
        events = self._fetch(since_date=since_date)
        if isinstance(events, str):
            return events
        matches = [
            e for e in events
            if (e.get("commitment_id") or "").strip() == commitment_id
        ]
        matches = _events_desc(matches)
        if limit and limit > 0:
            matches = matches[:limit]
        if not matches:
            return f"No instances found for commitment '{commitment_id}'."

        lines = [f"{len(matches)} instance(s) of '{commitment_id}' (newest first):"]
        for e in matches:
            line = f"• {e.get('date', '?')} {_status_of(e)} — {(e.get('title') or '(untitled)').strip()}"
            reason = (e.get("status_change_reason") or "").strip()
            if reason:
                line += f" | reason: {reason}"
            moved = (e.get("rescheduled_from") or "").strip()
            if moved:
                line += f" | moved from {moved}"
            lines.append(line)
        return "\n".join(lines)

    # ── GetPatternSummary ───────────────────────────────────────────────────
    def pattern_summary(
        self, commitment_id: str = "", category: str = "", since_date: str = ""
    ) -> str:
        commitment_id = (commitment_id or "").strip()
        category = (category or "").strip()
        if not commitment_id and not category:
            return "Error: provide either commitment_id or category."

        if commitment_id:
            events = self._fetch(since_date=since_date)
            if isinstance(events, str):
                return events
            events = [
                e for e in events
                if (e.get("commitment_id") or "").strip() == commitment_id
            ]
            label = f"commitment '{commitment_id}'"
        else:
            events = self._fetch(category=category, since_date=since_date)
            if isinstance(events, str):
                return events
            label = f"category '{category}'"

        if not events:
            return f"No events found for {label}."

        ordered = _events_desc(events)
        counts = Counter(_status_of(e) for e in ordered)
        total = len(ordered)

        # Most common status-change reason among canceled instances.
        reasons = Counter(
            (e.get("status_change_reason") or "").strip()
            for e in ordered
            if _status_of(e) == "canceled" and (e.get("status_change_reason") or "").strip()
        )
        top_reason = reasons.most_common(1)[0] if reasons else None

        # Current streak: from the most recent resolved instance, how many in a
        # row share that same status.
        resolved = [e for e in ordered if _status_of(e) in _TERMINAL_STATUSES]
        streak_status, streak_len = None, 0
        if resolved:
            streak_status = _status_of(resolved[0])
            for e in resolved:
                if _status_of(e) == streak_status:
                    streak_len += 1
                else:
                    break

        last_completed = next(
            (e.get("date") for e in ordered if _status_of(e) == "completed"), None
        )

        lines = [
            f"Pattern summary for {label} ({total} total):",
            f"• scheduled: {counts.get('scheduled', 0)}, "
            f"completed: {counts.get('completed', 0)}, "
            f"canceled: {counts.get('canceled', 0)}, "
            f"rescheduled: {counts.get('rescheduled', 0)}",
        ]
        if streak_status:
            lines.append(f"• current streak: {streak_len} {streak_status} in a row")
        if top_reason:
            lines.append(
                f"• most common status-change reason: \"{top_reason[0]}\" ({top_reason[1]}x)"
            )
        lines.append(f"• last completed: {last_completed or 'never'}")
        return "\n".join(lines)

    # ── GetRecentActivity ───────────────────────────────────────────────────
    def recent_activity(
        self, category: str = "", since_date: str = "", limit: int = 20
    ) -> str:
        events = self._fetch(category=(category or "").strip(), since_date=since_date)
        if isinstance(events, str):
            return events
        ordered = _events_desc(events)
        if limit and limit > 0:
            ordered = ordered[:limit]
        if not ordered:
            scope = f" in '{category}'" if category else ""
            return f"No recent activity{scope}."

        scope = f" in '{category}'" if category else ""
        lines = [f"{len(ordered)} recent event(s){scope} (newest first):"]
        for e in ordered:
            line = (
                f"• {e.get('date', '?')} [{_status_of(e)}] "
                f"{(e.get('title') or '(untitled)').strip()}"
            )
            cat = (e.get("category") or "").strip()
            if cat and not category:
                line += f" | {cat}"
            cid = (e.get("commitment_id") or "").strip()
            if cid:
                line += f" | id: {cid}"
            reason = (e.get("status_change_reason") or "").strip()
            if reason:
                line += f" | reason: {reason}"
            lines.append(line)
        return "\n".join(lines)
