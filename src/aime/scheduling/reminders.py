"""Model-facing reminder operations — the engine behind the CreateReminder /
ListReminders / DeleteReminder client tools.

A reminder is just a schedule record: a ``relative`` trigger (fire N days before
a linked event, at a wall-clock time) paired with a ``send_message`` action. It's
the exact same record the event-modal UI writes (``docs/scheduling.md`` §7), so a
reminder the model sets and one the user clicks in are indistinguishable — both
are evaluated by the one scheduler loop and both re-arm for free when the event
moves.

This module is pure orchestration over :class:`ScheduleStore`: no Flask, no
backend HTTP, no model. The controller calls it directly for the intercepted
reminder tools (like it calls the web-search sub-agent), and it returns plain
dicts/strings ready to hand back to the model.
"""

from __future__ import annotations

from typing import Callable

from .store import ScheduleStore, make_schedule, validate_schedule


def reminder_message_for(event: dict | None) -> str:
    """The send_message template for a reminder. The ``{event_*}`` tokens are
    rendered against the *live* event at fire time (see scheduler.render_message),
    so the text stays correct even if the event is later edited or moved."""
    if event and str(event.get("time") or "").strip():
        return "Reminder: {event_title} on {event_date} at {event_time}"
    return "Reminder: {event_title} on {event_date}"


def describe_lead(days_before: int, at_time: str | None) -> str:
    """A short human phrase for when a reminder fires, e.g. '3 days before at
    08:00' or 'on the day'. Used in the result handed back to the model so its
    confirmation to the user reads naturally."""
    if days_before <= 0:
        when = "on the day"
    else:
        when = f"{days_before} day{'s' if days_before != 1 else ''} before"
    return f"{when} at {at_time}" if at_time else when


class ReminderService:
    """Create / list / delete event reminders for one user.

    Bound to that user's :class:`ScheduleStore` and an ``events_lookup`` callable
    returning their upcoming events (used to validate the link and to title the
    confirmation). ``default_tz`` is the timezone a reminder is interpreted in
    when the caller doesn't supply one — callers should pass the user's live
    client timezone where they have it.
    """

    def __init__(
        self,
        store: ScheduleStore,
        events_lookup: Callable[[], list],
        *,
        default_tz: str = "UTC",
    ):
        self._store = store
        self._events = events_lookup
        self._default_tz = default_tz or "UTC"

    def _event_index(self) -> dict:
        try:
            return {e["id"]: e for e in (self._events() or []) if "id" in e}
        except Exception:
            return {}

    @staticmethod
    def _match(index: dict, event_id):
        """Look an event up by id, tolerating an int/str mismatch between what
        the model passed and what the backend keys on."""
        if event_id in index:
            return event_id, index[event_id]
        for key, ev in index.items():
            if str(key) == str(event_id):
                return key, ev
        return None, None

    def create(self, *, event_id, days_before, at_time=None, tz: str | None = None) -> dict:
        """Create a reminder linked to ``event_id``. Returns a result dict with
        ``ok`` plus, on success, the new ``reminder_id``, the event title, and a
        human ``lead`` phrase; on failure, a plain ``error`` string."""
        key, event = self._match(self._event_index(), event_id)
        if event is None:
            return {"ok": False, "error": f"No event with id {event_id}. "
                    "Look it up with FilterUsersEvents to confirm the id."}
        try:
            days = int(days_before)
        except (TypeError, ValueError):
            return {"ok": False, "error": "days_before must be a whole number."}
        trigger = {
            "kind": "relative",
            "event_id": key,
            "days_before": days,
            "at_time": at_time or None,
            "minutes_before": 0,
        }
        record = make_schedule(
            trigger=trigger,
            action={"kind": "send_message", "message": reminder_message_for(event)},
            tz=tz or self._default_tz,
            enabled=True,
            label="reminder",
        )
        err = validate_schedule(record)
        if err is not None:
            return {"ok": False, "error": err}
        if not self._store.save(record):
            return {"ok": False, "error": "Couldn't save the reminder."}
        return {
            "ok": True,
            "reminder_id": record["schedule_id"],
            "event_title": (event.get("title") or event.get("name") or "").strip(),
            "lead": describe_lead(days, at_time or None),
        }

    def list(self, *, event_id=None) -> list[dict]:
        """The user's reminders (relative schedules), optionally filtered to one
        event. Each item is flattened to the fields the model cares about."""
        out: list[dict] = []
        index = self._event_index()
        for sched in self._store.list_schedules():
            t = sched.get("trigger", {})
            if t.get("kind") != "relative":
                continue
            if event_id is not None and str(t.get("event_id")) != str(event_id):
                continue
            ev = index.get(t.get("event_id")) or {}
            out.append({
                "reminder_id": sched.get("schedule_id", ""),
                "event_id": t.get("event_id"),
                "event_title": (ev.get("title") or ev.get("name") or "").strip(),
                "days_before": t.get("days_before", 0),
                "at_time": t.get("at_time"),
                "enabled": bool(sched.get("enabled", True)),
                "lead": describe_lead(int(t.get("days_before", 0) or 0), t.get("at_time")),
            })
        return out

    def delete(self, reminder_id: str) -> dict:
        """Delete one reminder by id. Refuses ids that aren't relative reminders
        so this tool can't be turned into a way to drop scheduled-agent records."""
        record = self._store.load(reminder_id)
        if record is None:
            return {"ok": False, "error": "No reminder with that id."}
        if (record.get("trigger") or {}).get("kind") != "relative":
            return {"ok": False, "error": "That id isn't an event reminder."}
        return {"ok": bool(self._store.delete(reminder_id))}
