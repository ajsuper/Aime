"""Resolve the absolute dates in stored prose, without ever interpreting one.

Companion to `aime.freshness`. That module says how old a section is; this one
says what the dates *inside* it mean now — catching the case where the model
reads "the party on August 22, 2026" in a section edited yesterday and
announces a party that already happened.

**The rule that makes this safe: never interpret.** A resolver that guesses is
worse than no resolver, because a system-authored annotation carries authority
the model will trust over its own reading — the failure just moves from the
model into hardcoded logic and gets harder to see. So only dates that *cannot*
be misread are resolved:

    August 22, 2026   22 August 2026   Aug 22, 2026   2026-08-22

Every one carries an explicit 4-digit year and a worded or unambiguous month.
Anything else — `6/4`, `22/08`, a bare `August 22` with no year — is skipped
**silently**. There is no branch in this module that picks between two readings
of the same text, so there is nothing here that can be confidently wrong. A miss
costs an annotation; the freshness stamp still carries the section's age.

That set is exactly the form the system prompt mandates for persisted dates
(`Month DD, YYYY`), so compliance and staleness protection are the same work.

Like the freshness sidecar, the output is kept strictly *outside* the content:
woven in, it would break find/replace, and the model would eventually copy an
annotation back and persist it as fact.
"""

from __future__ import annotations

import datetime
import re

_SIDECAR_RE = re.compile(r"\n*<dates\b[^>]*>.*?</dates>[ \t]*\n?", re.DOTALL)

_MONTHS = ["January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December"]
_MONTH_NUM = {}
for _i, _name in enumerate(_MONTHS, start=1):
    _MONTH_NUM[_name.lower()] = _i
    _MONTH_NUM[_name[:3].lower()] = _i
_MONTH_ALT = "|".join(sorted(_MONTH_NUM, key=len, reverse=True))

_ORD = r"(?:st|nd|rd|th)?"

# Every pattern below requires a 4-digit year and an unambiguous month. No
# pattern matches a bare numeric pair — that omission is the whole design.
_PATTERNS = (
    # 2026-08-22
    re.compile(r"\b(?P<y>\d{4})-(?P<m>\d{2})-(?P<d>\d{2})\b"),
    # August 22, 2026 / Aug 22 2026 / August 22nd, 2026
    re.compile(rf"\b(?P<mon>{_MONTH_ALT})\.?\s+(?P<d>\d{{1,2}}){_ORD},?\s+(?P<y>\d{{4}})\b",
               re.IGNORECASE),
    # 22 August 2026 / 22nd Aug 2026
    re.compile(rf"\b(?P<d>\d{{1,2}}){_ORD}\s+(?P<mon>{_MONTH_ALT})\.?,?\s+(?P<y>\d{{4}})\b",
               re.IGNORECASE),
)

# A topic can hold a lot of dates; past this the sidecar costs more context than
# it saves. Truncation is reported rather than silent.
MAX_ENTRIES = 20


def _spell(d: datetime.date) -> str:
    return f"{_MONTHS[d.month - 1]} {d.day}, {d.year}"


def _relative(delta: int) -> str:
    if delta == 0:
        return "TODAY"
    if delta == 1:
        return "tomorrow"
    if delta == -1:
        return "yesterday (PAST)"
    if delta > 0:
        return f"in {delta} days"
    return f"{-delta} days ago (PAST)"


def find_dates(text: str) -> list[tuple[str, datetime.date]]:
    """Every unambiguously-written date in `text` as `(as_written, date)`, in
    order of first appearance, deduplicated by the date itself.

    A match that isn't a real calendar date (February 30, month 13) is dropped:
    it was never a date, so resolving it would be inventing one."""
    found: list[tuple[str, datetime.date]] = []
    seen_dates: set[datetime.date] = set()
    hits: list[tuple[int, str, datetime.date]] = []
    for pattern in _PATTERNS:
        for m in pattern.finditer(text):
            gd = m.groupdict()
            month = (int(gd["m"]) if gd.get("m")
                     else _MONTH_NUM.get(gd["mon"].lower().rstrip(".")))
            if not month:
                continue
            try:
                d = datetime.date(int(gd["y"]), month, int(gd["d"]))
            except ValueError:
                continue
            hits.append((m.start(), m.group(0), d))
    for _pos, raw, d in sorted(hits, key=lambda h: h[0]):
        if d in seen_dates:
            continue
        seen_dates.add(d)
        found.append((raw, d))
    return found


def sidecar(text: str, today: datetime.date) -> str:
    """The `<dates>` block for `text`, or '' when it holds no unambiguous date.

    Framed as something to verify, never as a source of truth: the calendar and
    the user are authoritative, and this is only a prompt to go check them."""
    found = find_dates(text or "")
    if not found:
        return ""
    shown = found[:MAX_ENTRIES]
    lines = [f'"{raw}" → {_relative((d - today).days)}' for raw, d in shown]
    if len(found) > len(shown):
        lines.append(f"({len(found) - len(shown)} more dates not listed)")
    return (
        f"<dates as-of {_spell(today)}>\n"
        "Dates written in this content, resolved against today so you don't have\n"
        "to count. A date marked PAST has already happened — do NOT describe it as\n"
        "upcoming. This is a prompt to check the calendar or ask the user, not a\n"
        "source of truth; dates written ambiguously are not listed here at all.\n"
        + "\n".join(lines)
        + "\n</dates>"
    )


def strip(text: str) -> str:
    """Remove any `<dates>` block from `text`.

    Applied to everything the model writes, so an annotation it copied out of
    its own context can never be persisted as though it were content."""
    if not text:
        return text
    return _SIDECAR_RE.sub("\n", text)
