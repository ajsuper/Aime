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
    elif name == "ReplaceTopic":
        parts.append(f"id={inp.get('id', '?')}")
        if inp.get("title"):
            parts.append(f"title={_truncate_for_log(inp['title'], 30)}")
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


def format_tool_response(name: str, result) -> str:
    """One-line description of what a tool call returned. Mirrors
    format_tool_details. Empty string if nothing notable."""
    if isinstance(result, dict) and "error" in result:
        return f"error: {_truncate_for_log(result.get('error'), 80)}"

    parts: list[str] = []

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
                  "ReplaceTopicContents", "EditTopicContents"):
        if isinstance(result, dict):
            for key in ("id", "status", "ok", "success"):
                if key in result:
                    parts.append(f"{key}={_truncate_for_log(result[key], 30)}")
            if not parts:
                keys = list(result.keys())[:3]
                if keys:
                    parts.append("keys: " + ", ".join(keys))
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
