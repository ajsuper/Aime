"""Human-readable one-line summaries of tool calls and their results.

UI-agnostic: every function here returns plain strings with no Rich/HTML/ANSI
markup. Frontends apply their own styling around the returned text. (The
assistant's own message text may contain Rich-style markup emitted by the
model itself; that's a separate concern handled by the frontend's renderer.)

The mapping from agent-side tool names to backend tool names (`TOOL_NAME_MAP`)
lives here too — it's domain knowledge about how the model's tool schemas
relate to the local tool server, not presentation.
"""

TOOL_NAME_MAP = {
    "FilterUsersEvents": "get_events",
    "EditEvent": "replace_event",
    "CreateEvent": "create_event",
    "FilterTopics": "get_topics",
    "CreateTopic": "create_topic",
    "ReplaceTopic": "replace_topic",
    "GetTopicContents": "get_topic_contents",
    "ReplaceTopicContents": "replace_topic_contents",
    "EditTopicContents": "edit_topic_contents",
    "RenameFolder": "rename_folder",
    "ListFolders": "list_folders",
}


def _truncate_for_log(value, limit: int = 60) -> str:
    if value is None:
        return ""
    s = str(value).replace("\n", " ").strip()
    if len(s) <= limit:
        return s
    return s[: limit - 1] + "…"


def format_tool_details(name: str, inp: dict) -> str:
    """One-line description of what a tool call is doing, based on its input.
    Empty string if nothing notable."""
    if not isinstance(inp, dict):
        return ""
    parts: list[str] = []

    if name == "FilterUsersEvents":
        if inp.get("filter_by_date"):
            parts.append(
                f"date {inp.get('start_date', '?')} → {inp.get('end_date', '?')}"
            )
        if inp.get("filter_by_category"):
            parts.append(f"category={inp.get('category')}")
        if inp.get("filter_by_title"):
            parts.append(f"title~'{_truncate_for_log(inp.get('title'), 30)}'")
        if inp.get("sort_order"):
            parts.append(f"sort={inp.get('sort_order')}")
    elif name == "CreateEvent":
        title = _truncate_for_log(inp.get("title"), 40) or "?"
        date_ = inp.get("date") or "?"
        parts.append(f"\"{title}\" on {date_}")
        if inp.get("time"):
            parts.append(f"at {inp['time']}")
        if inp.get("category"):
            parts.append(f"#{inp['category']}")
    elif name == "EditEvent":
        parts.append(f"id={inp.get('id', '?')}")
        for field in ("title", "date", "time", "category", "summary"):
            if inp.get(field):
                parts.append(f"{field}={_truncate_for_log(inp[field], 30)}")
    elif name == "FilterTopics":
        if inp.get("filter_by_category"):
            parts.append(f"category={inp.get('category')}")
        if inp.get("filter_by_title"):
            parts.append(f"title~'{_truncate_for_log(inp.get('title'), 30)}'")
        if not parts:
            parts.append("listing all")
    elif name == "CreateTopic":
        parts.append(f"\"{_truncate_for_log(inp.get('title'), 40) or '?'}\"")
        if inp.get("category"):
            parts.append(f"#{inp['category']}")
        if inp.get("folder"):
            parts.append(f"folder={_truncate_for_log(inp['folder'], 30)}")
    elif name == "ReplaceTopic":
        parts.append(f"id={inp.get('id', '?')}")
        if inp.get("title"):
            parts.append(f"title={_truncate_for_log(inp['title'], 30)}")
        if "folder" in inp:
            folder = inp.get("folder") or ""
            parts.append(f"folder={_truncate_for_log(folder, 30) or '(root)'}")
    elif name == "RenameFolder":
        parts.append(
            f"\"{_truncate_for_log(inp.get('old_name'), 30) or '?'}\""
            f" → \"{_truncate_for_log(inp.get('new_name'), 30) or '?'}\""
        )
    elif name == "WebSearch":
        q = _truncate_for_log(inp.get("request"), 60)
        if q:
            parts.append(f"\"{q}\"")
    elif name == "SendMessage":
        body = _truncate_for_log(inp.get("text"), 50)
        if body:
            parts.append(f"\"{body}\"")
    elif name in ("GetCommitmentHistory", "GetPatternSummary", "GetRecentActivity"):
        if inp.get("commitment_id"):
            parts.append(f"commitment={inp['commitment_id']}")
        if inp.get("category"):
            parts.append(f"category={inp['category']}")
        if inp.get("since_date"):
            parts.append(f"since {inp['since_date']}")
        if inp.get("limit"):
            parts.append(f"limit={inp['limit']}")
    elif name == "GetTopicContents":
        parts.append(f"id={inp.get('id', '?')}")
    elif name == "ReplaceTopicContents":
        parts.append(f"id={inp.get('id', '?')}")
        if inp.get("contents") is not None:
            parts.append(f"len={len(str(inp['contents']))}")
    elif name == "EditTopicContents":
        parts.append(f"id={inp.get('id', '?')}")
        if "old_string" in inp:
            parts.append(f"old='{_truncate_for_log(inp.get('old_string'), 30)}'")
        if "new_string" in inp:
            parts.append(f"new='{_truncate_for_log(inp.get('new_string'), 30)}'")
    else:
        for key in ("query", "url", "command", "path", "pattern"):
            if inp.get(key):
                parts.append(f"{key}=\"{_truncate_for_log(inp[key], 60)}\"")
        if not parts:
            keys = [k for k in inp.keys() if k != "tool_name"][:3]
            if keys:
                parts.append("with " + ", ".join(keys))

    return ", ".join(parts)


def _render_events(events: list) -> str:
    n = len(events)
    if n == 0:
        return "No events match the filters."
    lines = [f"{n} event{'s' if n != 1 else ''}:"]
    for ev in events:
        if not isinstance(ev, dict):
            continue
        eid = ev.get("id", "?")
        title = (ev.get("title") or "(untitled)").strip()
        when = ev.get("date") or "?"
        if ev.get("time"):
            when += f" {ev['time']}"
        head = f"• #{eid} {title} | {when}"
        if ev.get("category"):
            head += f" | {ev['category']}"
        # Show the status only when it's something other than a plain pending
        # `scheduled` — keeps the common case terse while making completed,
        # canceled, and `unknown` (a past event swept from `scheduled` because
        # its date/time passed unresolved) visible so the model isn't blind to
        # outcomes and knows when to ask the user how something went.
        status = (ev.get("status") or "scheduled").strip() or "scheduled"
        if status != "scheduled":
            head += f" | {status}"
        if ev.get("archived"):
            head += " | [archived]"
        lines.append(head)
        summary = (ev.get("summary") or "").strip()
        for sline in summary.splitlines():
            lines.append(f"    {sline}")
    return "\n".join(lines)


def _render_topics(topics: list) -> str:
    n = len(topics)
    if n == 0:
        return "No topics match the filters."
    lines = [f"{n} topic{'s' if n != 1 else ''}:"]
    for tp in topics:
        if not isinstance(tp, dict):
            continue
        tid = tp.get("id", "?")
        title = (tp.get("title") or tp.get("name") or "(untitled)").strip()
        head = f"• #{tid} {title}"
        if tp.get("category"):
            head += f" | {tp['category']}"
        folder = (tp.get("folder") or "").strip()
        head += f" | folder: {folder}" if folder else " | (root)"
        lines.append(head)
        summary = (tp.get("summary") or "").strip()
        for sline in summary.splitlines():
            lines.append(f"    {sline}")
    return "\n".join(lines)


def format_tool_result_for_model(name: str, result):
    """Render get-events / get-topics results as a compact text view for the
    model instead of raw JSON. Returns None for every other tool, signalling
    the caller to send the raw result unchanged.

    Errors on these two tools are still surfaced to the model — as a clean
    `Error: ...` line so it can explain the failure and help the user — rather
    than dropped or dumped as raw JSON.

    JSON serialization of these list results is token-heavy: each item repeats
    field names, quotes, and braces, and escapes every markdown newline in a
    summary as a literal `\\n`. A flat text layout keeps every field the model
    needs to act on — crucially the id — while shedding that syntactic
    overhead, which is the bulk of the cached-context cost on read turns."""
    if name not in ("FilterUsersEvents", "FilterTopics"):
        return None
    if isinstance(result, dict) and "error" in result:
        return f"Error: {result.get('error')}"
    if name == "FilterUsersEvents":
        if isinstance(result, list):
            return _render_events(result)
        if isinstance(result, dict):
            return _render_events(result.get("events") or [])
    else:  # FilterTopics
        if isinstance(result, list):
            return _render_topics(result)
        if isinstance(result, dict):
            return _render_topics(result.get("topics") or [])
    return None


def format_tool_response(name: str, result) -> str:
    """One-line description of what a tool call returned. Mirrors
    format_tool_details. Empty string if nothing notable."""
    if isinstance(result, dict) and "error" in result:
        return f"error: {_truncate_for_log(result.get('error'), 80)}"

    parts: list[str] = []

    if name == "ListFolders":
        if isinstance(result, dict):
            folders = result.get("folders") or []
            parts.append(f"{len(folders)} folder{'s' if len(folders) != 1 else ''}")
            for f in folders[:5]:
                if isinstance(f, dict):
                    parts.append(f"{f.get('name', '?')}({f.get('count', '?')})")
            if len(folders) > 5:
                parts.append(f"+{len(folders) - 5} more")
        return ", ".join(parts)
    if name in ("FilterUsersEvents", "FilterTopics"):
        if isinstance(result, list):
            items = result
        elif isinstance(result, dict):
            items = result.get("events") or result.get("topics") or []
        else:
            items = []
        label = "event" if name == "FilterUsersEvents" else "topic"
        parts.append(f"{len(items)} {label}{'s' if len(items) != 1 else ''}")
        for it in items[:3]:
            if not isinstance(it, dict):
                continue
            title = _truncate_for_log(it.get("title") or it.get("name"), 30) or "?"
            if name == "FilterUsersEvents":
                parts.append(f"\"{title}\"@{it.get('date', '?')}")
            else:
                tid = it.get("id")
                parts.append(f"\"{title}\"" + (f"#{tid}" if tid is not None else ""))
        if len(items) > 3:
            parts.append(f"+{len(items) - 3} more")
    elif name in ("CreateEvent", "EditEvent", "CreateTopic", "ReplaceTopic",
                  "ReplaceTopicContents", "EditTopicContents", "RenameFolder"):
        if isinstance(result, dict):
            for key in ("id", "status", "ok", "success"):
                if key in result:
                    parts.append(f"{key}={_truncate_for_log(result[key], 30)}")
            if not parts:
                keys = list(result.keys())[:3]
                if keys:
                    parts.append("keys: " + ", ".join(keys))
    elif name in ("GetCommitmentHistory", "GetPatternSummary", "GetRecentActivity"):
        # These tools return a text digest; its first line is the headline
        # (e.g. "5 instance(s) of 'bouldering'…" or "Pattern summary for…").
        first_line = str(result).splitlines()[0] if result else ""
        parts.append(_truncate_for_log(first_line, 80))
    elif name == "GetTopicContents":
        if isinstance(result, dict):
            contents = result.get("contents", "") or ""
            parts.append(f"len={len(str(contents))}")
            preview = _truncate_for_log(contents, 50)
            if preview:
                parts.append(f"preview='{preview}'")
    else:
        if isinstance(result, list):
            parts.append(f"{len(result)} item{'s' if len(result) != 1 else ''}")
        elif isinstance(result, dict):
            for k, v in list(result.items())[:3]:
                parts.append(f"{k}={_truncate_for_log(v, 40)}")
        else:
            preview = _truncate_for_log(result, 60)
            if preview:
                parts.append(preview)

    return ", ".join(parts)
