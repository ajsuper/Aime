"""`[event-N]` references: let a topic point at an event instead of restating it.

The freshness stamps and the date sidecar both make staleness *visible*. This
makes one class of it impossible: a topic that says "the party is [event-42]"
holds no date of its own, so there is nothing to go stale. Move the event and
the topic follows; cancel it and the topic says canceled.

That is `aime.scheduling.reminders` generalized. A reminder stores the template
"{event_title} on {event_date}" and renders it against the *live* event when it
fires, which is why a reminder never announces a date the calendar has since
moved. A topic reference behaves the same way, with the topic body as the
template.

**Not load-bearing, and deliberately so.** Unlike `[graphic-N]`, which is the
only way to put a chart in a topic, nothing stops the model typing a date as
prose — so this can only ever be a quality layer on top of the stamps, never a
substitute for them. It earns its place where it is used, and costs nothing
where it isn't.

Like every other annotation here, resolutions go in a sidecar *outside* the
content. Expanding them inline would break `EditTopicContents`: the model
builds its `find` out of what it read, and a rendered expansion that isn't in
the file makes the patch miss.
"""

from __future__ import annotations

import datetime
import re

# A reference to one of the user's own events. Bare integer id — the same id
# FilterUsersEvents returns and the event tools take.
TAG_RE = re.compile(r"\[event-(\d+)\]")

_SIDECAR_RE = re.compile(r"\n*<events\b[^>]*>.*?</events>[ \t]*\n?", re.DOTALL)

_DATE_FMT = "%d/%m/%Y"
_DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday",
         "Sunday"]
_MONTHS = ["January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December"]

# Past this, resolving costs more context than the references are worth.
MAX_REFS = 25


def event_ids(text: str) -> list[int]:
    """Every `[event-N]` id in `text`, in order, deduplicated."""
    out: list[int] = []
    seen: set[int] = set()
    for m in TAG_RE.finditer(text or ""):
        n = int(m.group(1))
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _when(ev: dict, today: datetime.date) -> str:
    raw = (ev.get("date") or "").strip()
    try:
        d = datetime.datetime.strptime(raw, _DATE_FMT).date()
    except ValueError:
        return raw or "no date"
    text = f"{_DAYS[d.weekday()]}, {_MONTHS[d.month - 1]} {d.day}, {d.year}"
    time_ = (ev.get("time") or "").strip()
    if time_:
        text += f" at {time_}"
    delta = (d - today).days
    if delta == 0:
        rel = "TODAY"
    elif delta == 1:
        rel = "tomorrow"
    elif delta == -1:
        rel = "yesterday (PAST)"
    elif delta > 0:
        rel = f"in {delta} days"
    else:
        rel = f"{-delta} days ago (PAST)"
    return f"{text} · {rel}"


def sidecar(text: str, events: dict, today: datetime.date) -> str:
    """The `<events>` block resolving the references in `text`.

    `events` maps id -> event dict. A referenced id that isn't there is
    reported as unresolved rather than omitted: silence would read as "no such
    reference", when what actually happened is the event was deleted and the
    topic is now pointing at nothing — which the model should tell the user
    about, or clean up.
    """
    ids = event_ids(text)
    if not ids:
        return ""
    shown = ids[:MAX_REFS]
    lines = []
    for n in shown:
        ev = events.get(n) or events.get(str(n))
        if not isinstance(ev, dict):
            lines.append(f"[event-{n}] → no such event (deleted?) — tell the "
                         "user or remove the reference")
            continue
        title = (ev.get("title") or "(untitled)").strip()
        parts = [f'"{title}"', _when(ev, today)]
        status = (ev.get("status") or "scheduled").strip() or "scheduled"
        if status != "scheduled":
            parts.append(status.upper())
        if ev.get("archived"):
            parts.append("archived")
        lines.append(f"[event-{n}] → " + " — ".join(parts))
    if len(ids) > len(shown):
        lines.append(f"({len(ids) - len(shown)} more references not resolved)")
    return (
        "<events as-of " + f"{_MONTHS[today.month - 1]} {today.day}, {today.year}>\n"
        "Live calendar state for the [event-N] references above. These are read\n"
        "fresh every time, so they are current even when the surrounding prose\n"
        "is not — prefer them over any date written nearby.\n"
        + "\n".join(lines)
        + "\n</events>"
    )


def strip(text: str) -> str:
    """Remove any `<events>` block from `text`. The tags themselves are content
    and are left alone — only our resolution of them is removed."""
    if not text:
        return text
    return _SIDECAR_RE.sub("\n", text)
