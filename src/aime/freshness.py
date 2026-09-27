"""Per-section freshness stamps: let stored prose carry its own age.

The model writes "Party in 3 days on August 22nd" into a topic, reads it back a
week later, and announces the party as upcoming. Topics are the only free-form
store and they carry no timestamps, so nothing in the payload marks that
sentence as old. The worst case is About Me / Pending, injected verbatim into
the cached system prefix every session with the model told not to re-fetch them.

The fix deliberately does not read the prose. Detecting relative phrases ("in 3
days", "next week") is fuzzy and language-bound — it needs a pattern set per
language and still misses paraphrases. Instead the *system* records when each
section was last written, and hands that back on every read. "In 3 days" then
becomes resolvable no matter how it was worded, in any language, including
phrasings nobody anticipated.

**Why a trailing block and not an inline marker.** The obvious design puts a
stamp under each heading. It cannot work: `EditTopicContents` is find/replace
and is the prompted default, so the model builds a `find` out of the text it
just read. If a read renders `## Social  (last updated ...)` while the file
holds `## Social` plus a marker, every `find` spanning a heading misses, and the
primary edit path breaks. So stamps live in ONE block at the very end of the
file and the content stream above stays byte-for-byte what the model saw.

Sections are split on markdown ATX headings, keyed by heading text. A key with
no matching heading (a renamed section) is dropped rather than guessed at — an
unstamped section costs an annotation, a mis-keyed one would assert something
false, and the whole point here is to never do that.
"""

from __future__ import annotations

import datetime
import re

# The stored block. Sits at the end of the file, regenerated wholesale by the
# backend on every write, so a mangled or duplicated one self-heals.
BLOCK_OPEN = "<!--aime:updated v1"
BLOCK_CLOSE = "-->"
_BLOCK_RE = re.compile(
    r"\n*" + re.escape(BLOCK_OPEN) + r"\n(.*?)\n?" + re.escape(BLOCK_CLOSE) + r"[ \t]*\n?",
    re.DOTALL,
)

# The rendered sidecar handed to the model. Also matched on the way back in, so
# a model that copies its own context into a write cannot persist it.
SIDECAR_OPEN = "<freshness"
_SIDECAR_RE = re.compile(r"\n*<freshness\b[^>]*>.*?</freshness>[ \t]*\n?", re.DOTALL)

# A markdown ATX heading (`# x` … `###### x`), at line start.
_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.*?)[ \t]*$", re.MULTILINE)

# Reserved key for content before the first heading.
PREAMBLE_KEY = "~preamble"

_MONTHS = ["January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December"]


def _spell(d: datetime.date) -> str:
    """`Month DD, YYYY` — the one canonical persisted form (see the system
    prompt's Dates & times section)."""
    return f"{_MONTHS[d.month - 1]} {d.day}, {d.year}"


def section_key(heading_text: str, seen: dict) -> str:
    """A stable key for a heading. `=` and newlines are stripped because they
    delimit the stored block; a repeated heading gets a `~2`, `~3` … suffix so
    two sections of the same name stay distinguishable."""
    base = heading_text.replace("=", "").replace("\n", "").strip() or "~untitled"
    n = seen.get(base, 0) + 1
    seen[base] = n
    return base if n == 1 else f"{base}~{n}"


def split_sections(body: str) -> list[tuple[str, int, int]]:
    """`(key, start, end)` for each section of `body`, in order.

    A section runs from its heading line to just before the next heading.
    Content before the first heading is the preamble, present only when it holds
    something other than whitespace."""
    out: list[tuple[str, int, int]] = []
    seen: dict[str, int] = {}
    matches = list(_HEADING_RE.finditer(body))
    if not matches:
        return [(PREAMBLE_KEY, 0, len(body))] if body.strip() else []
    if body[: matches[0].start()].strip():
        out.append((PREAMBLE_KEY, 0, matches[0].start()))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        out.append((section_key(m.group(2), seen), m.start(), end))
    return out


def parse(content: str) -> tuple[str, dict[str, str]]:
    """Split stored content into `(body, {key: 'YYYY-MM-DD'})`.

    Unparseable lines are skipped rather than raising: the stamps are an
    annotation, and a corrupt block must never make a topic unreadable."""
    stamps: dict[str, str] = {}
    body = content
    m = _BLOCK_RE.search(content)
    if m:
        body = content[: m.start()] + content[m.end():]
        for line in m.group(1).splitlines():
            key, sep, value = line.partition("=")
            value = value.strip()
            if not sep or not key.strip() or not _valid_iso(value):
                continue
            stamps[key.strip()] = value
    return body, stamps


def _valid_iso(value: str) -> bool:
    try:
        datetime.date.fromisoformat(value)
    except ValueError:
        return False
    return True


def render_block(stamps: dict[str, str]) -> str:
    """The stored block for `stamps`, or '' when there is nothing to record."""
    if not stamps:
        return ""
    lines = "\n".join(f"{k}={v}" for k, v in stamps.items())
    return f"{BLOCK_OPEN}\n{lines}\n{BLOCK_CLOSE}\n"


def strip(text: str) -> str:
    """Remove both the stored block and any rendered sidecar from `text`.

    Applied to everything the model writes. Both shapes are machine-generated,
    so this is exact matching with no natural-language guesswork — it exists
    solely to stop the model persisting a copy of its own context, the same
    hazard the graphics module handles with sentinel strips.

    Removal only: surrounding whitespace is left exactly as found, because this
    also runs over find/replace *fragments*, where normalizing a trailing
    newline would silently stop a patch matching."""
    if not text:
        return text
    return _BLOCK_RE.sub("\n", _SIDECAR_RE.sub("\n", text))


def _ago(days: int) -> str:
    if days <= 0:
        return "today"
    if days == 1:
        return "yesterday"
    return f"{days} days ago"


def sidecar(content: str, today: datetime.date) -> str:
    """The `<freshness>` block appended to a topic read, or '' when nothing is
    stamped.

    Kept *outside* the content — never woven into it — for the two reasons that
    shape this whole module: the content the model reads must stay identical to
    what is stored so find/replace keeps working, and an annotation it can copy
    back into a write is an annotation that eventually gets persisted as fact.

    Each line gives the absolute date *and* the elapsed days. The absolute date
    is what makes it checkable; the elapsed count is there so the model never
    has to do the subtraction itself, which is the arithmetic it gets wrong."""
    body, stamps = parse(content)
    if not stamps:
        return ""
    lines = []
    for key, _start, _end in split_sections(body):
        iso = stamps.get(key)
        if not iso:
            continue
        try:
            d = datetime.date.fromisoformat(iso)
        except ValueError:
            continue
        label = "the opening text" if key == PREAMBLE_KEY else f'"{key}"'
        lines.append(f"{label} — last written {_spell(d)} ({_ago((today - d).days)})")
    if not lines:
        return ""
    return (
        f"<freshness as-of {_spell(today)}>\n"
        "How old each part of this topic is. Anything written relative to when it\n"
        "was saved — \"in 3 days\", \"next week\", \"tomorrow\" — must be read against\n"
        "that date, not today's. Re-check anything time-sensitive before repeating\n"
        "it to the user.\n"
        + "\n".join(lines)
        + "\n</freshness>"
    )


def for_model(content: str, today: datetime.date) -> str:
    """A topic's contents as the model should see them: the body exactly as
    stored (so find/replace still works), with the freshness sidecar appended."""
    body, _ = parse(content)
    body = body.rstrip()
    side = sidecar(content, today)
    return f"{body}\n\n{side}" if side else body


def restamp(old_content: str, new_content: str, stamp_date: str) -> str:
    """`new_content` with its stamp block rebuilt: any section whose text
    changed is stamped `stamp_date`, the rest keep the date they had.

    Stamping only what changed is the point. Editing one line must not make the
    whole topic look freshly written — that is the unsafe direction, where old
    text starts reading as new and the staleness this module exists to surface
    goes back into hiding.

    `stamp_date` is supplied by the caller as a ready-made 'YYYY-MM-DD' string:
    this module never asks the clock what day it is, so the user's timezone
    stays the caller's business.
    """
    old_body, old_stamps = parse(old_content or "")
    new_body, carried = parse(new_content or "")
    # A stamp already inside the incoming content is not authoritative — the
    # model may have echoed one back. Prefer what was actually stored before.
    # Compared with surrounding whitespace normalized away: a blank line
    # gained or lost at a section boundary is cosmetic, and restamping on it
    # would report a write that never happened.
    old_text = {k: old_body[s:e].strip() for k, s, e in split_sections(old_body)}

    stamps: dict[str, str] = {}
    for key, start, end in split_sections(new_body):
        if key in old_text and old_text[key] == new_body[start:end].strip():
            prior = old_stamps.get(key) or carried.get(key)
            stamps[key] = prior if prior and _valid_iso(prior) else stamp_date
        else:
            stamps[key] = stamp_date
    block = render_block(stamps)
    body = new_body.rstrip()
    return f"{body}\n\n{block}" if block else body + "\n"
