"""Weekday checksum: verify the weekday the model asserts against the date it sent.

The model has one temporal anchor — the per-turn `<clock>` block — and resolves
"next Tuesday" by unassisted mental arithmetic against it. When that arithmetic
slips, the wrong date is written to the calendar silently: nothing downstream
can tell "25/08/2026" (what the user meant) from "23/08/2026" (what the model
computed), because both are well-formed dates.

So we make the model *show its work*. `weekday` is required on both event-write
schemas, which means every write asserts two independently-derived facts about
the same day. A slip in the arithmetic makes them disagree, and disagreement is
mechanically detectable — no guessing at intent, no natural-language parsing.
This is the same trick a checksum digit plays on a card number.

Deliberately NOT stored: the weekday is a *property* of the date, so persisting
it would create a second source of truth that could itself drift. It is consumed
here and popped from the payload before the request reaches the backend.

Absence skips the check. The model always sends it (the schema makes it
required), but the view-side paths that call the gateway directly — the web UI's
own event create/edit — speak the backend vocabulary and have no reason to
assert a weekday they computed from a real date picker.
"""

import datetime

_DATE_FMT = "%d/%m/%Y"

# Indexed to match datetime.date.weekday() — Monday is 0.
_DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday",
              "Friday", "Saturday", "Sunday"]
_BY_NAME = {name.lower(): i for i, name in enumerate(_DAY_NAMES)}


class WeekdayError(ValueError):
    """Raised when the asserted weekday doesn't fall on the given date.

    Message is model-facing — it names both candidate corrections so the model
    can fix the date itself rather than guessing again."""


def _parse_date(raw):
    """The date as a `date`, or None if it isn't a well-formed DD/MM/YYYY.

    None means "can't check" rather than "invalid": a malformed date has its own
    error paths (the backend's own validation, or event_length's), and shadowing
    those with a confusing weekday complaint would send the model chasing the
    wrong problem."""
    if not isinstance(raw, str):
        return None
    try:
        return datetime.datetime.strptime(raw.strip(), _DATE_FMT).date()
    except ValueError:
        return None


def check_weekday(payload: dict) -> dict:
    """Verify `weekday` against `date`, returning a NEW dict with `weekday`
    consumed. Raises WeekdayError when they disagree.

    A missing/blank `weekday`, a missing `date`, or a date that doesn't parse
    all skip the check silently — see the module docstring and `_parse_date`.
    """
    out = dict(payload)
    claimed_raw = out.pop("weekday", None)
    if claimed_raw in (None, ""):
        return out

    claimed = _BY_NAME.get(str(claimed_raw).strip().lower())
    if claimed is None:
        raise WeekdayError(
            f"'{claimed_raw}' is not a weekday. Use one of: "
            + ", ".join(_DAY_NAMES) + "."
        )

    date = _parse_date(out.get("date"))
    if date is None:
        return out

    actual = date.weekday()
    if actual == claimed:
        return out

    # The two dates the model plausibly meant: the nearest such weekday on
    # either side. Anchoring on the claimed date (not on "today") keeps this
    # pure and unambiguous — "the Tuesday of that week" would beg the question
    # of where the week starts, which is exactly the kind of implicit
    # convention this whole module exists to eliminate.
    back = (actual - claimed) % 7          # 1..6 — never 0, they differ
    forward = (claimed - actual) % 7       # 1..6
    prev = date - datetime.timedelta(days=back)
    nxt = date + datetime.timedelta(days=forward)
    name = _DAY_NAMES[claimed]
    raise WeekdayError(
        f"{date.strftime(_DATE_FMT)} is a {_DAY_NAMES[actual]}, not a {name}. "
        f"The nearest {name}s are {prev.strftime(_DATE_FMT)} "
        f"({back} day{'s' if back != 1 else ''} earlier) and "
        f"{nxt.strftime(_DATE_FMT)} ({forward} day{'s' if forward != 1 else ''} "
        "later). Re-send with the date you actually mean — and if it isn't "
        "clear which the user meant, ask them rather than picking one."
    )
