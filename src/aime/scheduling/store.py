"""Persistence + validation for schedule records.

A *schedule record* pairs a ``trigger`` (when to fire) with an ``action`` (what
to do) — the unified shape behind both scheduled agents and event reminders (see
``docs/scheduling.md`` §1). This module owns three things:

* :class:`ScheduleStore` — encrypted per-user CRUD, a near-exact clone of
  ``agents.AgentDefinitionStore``: one file per record under the user's
  ``schedules/`` dir, sealed with the user's DEK and the ``schedule_id`` as AEAD
  associated data. All IO is best-effort (a failed write returns ``False``).
* :func:`make_schedule` / :func:`new_schedule_id` — build a fresh record with
  state and timestamps stamped in.
* :func:`validate_schedule` — enforce the tagged-variant invariants so illegal
  records never reach disk *and* so a web handler can return a precise reason.

Validation is kept separate from ``save`` on purpose: a caller validates first
(to surface *why* a record was rejected) and ``save`` re-checks as a last line
of defense, returning ``False`` rather than raising.
"""

import datetime
import hashlib
import json
import os
import re
import zoneinfo

from cryptography.exceptions import InvalidTag

from .. import encryption as _enc


_SUFFIX = ".json.enc"
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

_TRIGGER_KINDS = ("recurring", "once", "relative")
_ACTION_KINDS = ("send_message", "run_agent")
_FREQS = ("daily", "weekly", "monthly")


def _utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def new_schedule_id(label: str = "") -> str:
    """A stable, unique, filesystem-safe id: ``sch-<slug>-<rand>``. The slug is
    derived from an optional human label purely for on-disk readability;
    uniqueness comes from the random suffix."""
    slug = "".join(c if c.isalnum() or c in "-_" else "-" for c in label).strip("-")
    slug = (slug[:40] or "sch").lower()
    rand = hashlib.sha1(os.urandom(8)).hexdigest()[:8]
    return f"sch-{slug}-{rand}"


def make_schedule(
    *,
    trigger: dict,
    action: dict,
    tz: str,
    enabled: bool = True,
    label: str = "",
    schedule_id: str | None = None,
) -> dict:
    """Build a fresh schedule record, stamping id, empty state, and timestamps.

    ``trigger`` and ``action`` are stored as given — call :func:`validate_schedule`
    before persisting. ``state`` starts all-null: a recurring schedule with no
    ``last_run_at`` first fires at its next slot, never retroactively."""
    now = _utc_now_iso()
    return {
        "schedule_id": schedule_id or new_schedule_id(label),
        "enabled": bool(enabled),
        "tz": tz,
        "trigger": dict(trigger),
        "action": dict(action),
        "state": {"last_run_at": None, "fired_at": None, "sent_for_start": None},
        "created_at": now,
        "updated_at": now,
    }


def _valid_tz(tz) -> bool:
    if not isinstance(tz, str) or not tz:
        return False
    try:
        zoneinfo.ZoneInfo(tz)
        return True
    except Exception:
        return False


def _validate_trigger(t: dict) -> str | None:
    if not isinstance(t, dict):
        return "trigger must be an object"
    kind = t.get("kind")
    if kind not in _TRIGGER_KINDS:
        return f"trigger.kind must be one of {_TRIGGER_KINDS}"

    # The core link invariant: an event_id is meaningful only for `relative`.
    has_event = t.get("event_id") is not None
    if kind == "relative":
        if not has_event:
            return "relative trigger requires event_id"
        days = t.get("days_before", 0)
        if not isinstance(days, int) or not (0 <= days <= 366):
            return "days_before must be an int in 0..366"
        at_time = t.get("at_time")
        if at_time is not None and not _HHMM.match(str(at_time)):
            return "at_time must be HH:MM or null"
        mins = t.get("minutes_before", 0)
        if not isinstance(mins, int) or mins < 0:
            return "minutes_before must be a non-negative int"
        return None

    if has_event:
        return f"{kind} trigger must not set event_id"

    if kind == "recurring":
        if t.get("freq") not in _FREQS:
            return f"recurring.freq must be one of {_FREQS}"
        if not _HHMM.match(str(t.get("time", ""))):
            return "recurring.time must be HH:MM"
        if t["freq"] == "weekly":
            wd = t.get("weekday")
            if not isinstance(wd, int) or not (1 <= wd <= 7):
                return "weekly recurring requires weekday in 1..7"
        if t["freq"] == "monthly":
            day = t.get("day")
            if not isinstance(day, int) or not (1 <= day <= 31):
                return "monthly recurring requires day in 1..31"
        return None

    if kind == "once":
        try:
            datetime.datetime.fromisoformat(str(t.get("at")))
        except (TypeError, ValueError):
            return "once.at must be an ISO datetime"
        return None

    return None  # unreachable


def _validate_action(a: dict) -> str | None:
    if not isinstance(a, dict):
        return "action must be an object"
    kind = a.get("kind")
    if kind not in _ACTION_KINDS:
        return f"action.kind must be one of {_ACTION_KINDS}"
    if kind == "run_agent":
        if not a.get("agent_id"):
            return "run_agent action requires agent_id"
        if a.get("message"):
            return "run_agent action must not set message"
    if kind == "send_message":
        if not a.get("message"):
            return "send_message action requires message"
        if a.get("agent_id"):
            return "send_message action must not set agent_id"
    return None


def validate_schedule(record: dict) -> str | None:
    """Return a human-readable reason the record is invalid, or ``None`` if it's
    well-formed. Enforces the tagged-variant invariants from ``docs/scheduling.md``
    so a field can never sit in a variant that doesn't use it."""
    if not isinstance(record, dict):
        return "record must be an object"
    if not record.get("schedule_id"):
        return "record requires a schedule_id"
    if not _valid_tz(record.get("tz")):
        return "tz must be a valid IANA timezone"
    return _validate_trigger(record.get("trigger")) or _validate_action(record.get("action"))


class ScheduleStore:
    """Reads and writes encrypted schedule records for one user.

    Mirrors ``AgentDefinitionStore``: one file per record, encrypted with the
    user's DEK and the ``schedule_id`` as associated data. IO is best-effort — a
    failed write returns ``False`` and an unreadable file is skipped — so a
    storage hiccup can't take down the tick loop or a request handler."""

    def __init__(self, schedules_dir: str, dek: bytes):
        self._dir = schedules_dir
        self._dek = dek

    def _path(self, schedule_id: str) -> str:
        return os.path.join(self._dir, f"{schedule_id}{_SUFFIX}")

    def save(self, record: dict) -> bool:
        """Persist a record (refreshing ``updated_at``). Returns ``False`` on any
        failure, including a record that fails :func:`validate_schedule` — so an
        invalid record never reaches disk even if a caller skipped validation."""
        schedule_id = record.get("schedule_id")
        if not schedule_id or validate_schedule(record) is not None:
            return False
        record = {**record, "updated_at": _utc_now_iso()}
        try:
            os.makedirs(self._dir, exist_ok=True)
            path = self._path(schedule_id)
            plaintext = json.dumps(record).encode("utf-8")
            blob = _enc.encrypt_blob(self._dek, plaintext, aad=schedule_id.encode("utf-8"))
            tmp = f"{path}.{os.getpid()}.tmp"
            with open(tmp, "wb") as f:
                f.write(blob)
            os.replace(tmp, path)
            return True
        except (OSError, TypeError, ValueError):
            return False

    def load(self, schedule_id: str) -> dict | None:
        """Decrypt and return one record, or ``None`` if missing/unreadable."""
        try:
            with open(self._path(schedule_id), "rb") as f:
                blob = f.read()
            plaintext = _enc.decrypt_blob(self._dek, blob, aad=schedule_id.encode("utf-8"))
            return json.loads(plaintext.decode("utf-8"))
        except (OSError, ValueError, InvalidTag):
            # Missing, corrupt, or AAD-mismatched (wrong id) — treat as unreadable.
            return None

    def list_schedules(self) -> list[dict]:
        """Every schedule for this user, newest first. Unreadable files are
        skipped rather than failing the listing."""
        try:
            names = os.listdir(self._dir)
        except OSError:
            return []
        out: list[dict] = []
        for name in names:
            if not name.endswith(_SUFFIX):
                continue
            record = self.load(name[: -len(_SUFFIX)])
            if record is not None:
                out.append(record)
        out.sort(key=lambda r: r.get("created_at", ""), reverse=True)
        return out

    def delete(self, schedule_id: str) -> bool:
        """Remove a schedule. Returns ``True`` if a file was deleted, ``False`` if
        it didn't exist or couldn't be removed."""
        try:
            os.remove(self._path(schedule_id))
            return True
        except OSError:
            return False
