"""The scheduler core: one tick loop that fires due schedule records.

A single daemon thread (the deployment is single-process and threaded, so
nothing double-fires) wakes every ``tick_seconds``, walks every user's
:class:`~aime.scheduling.store.ScheduleStore`, and fires whatever is due. All
the "what does due mean" math lives in :mod:`recurrence`; all the "what does
firing *do*" lives behind injected callables — so this module imports neither
``frontends`` nor the messaging/agent stacks and stays unit-testable with fakes.

Firing decisions are recomputed every tick from durable state
(``last_run_at`` / ``fired_at`` / ``sent_for_start``); no ``next_run_at`` is
ever stored, so a crash mid-tick re-derives identically on reboot. Robustness is
layered: a bad user can't break another user's tick, and a bad schedule can't
break the rest of that user's.

Injected dependencies (see ``docs/scheduling.md`` §4):

* ``auth``            — object with ``list_users()`` and ``get_dek(user_id)``.
* ``schedules_dir``   — ``user_id -> path`` to that user's ``schedules/`` dir.
* ``run_agent``       — ``(agent_id, user_id, tz) -> None``.
* ``send_message``    — ``(user_id, text) -> None`` (resolves contact + channel).
* ``upcoming_events`` — ``(user_id) -> list[dict]`` with ``id``/``date``/``time``/``title``.
"""

from __future__ import annotations

import datetime
import logging
import threading
from typing import Callable

from ..auth import BackgroundUnavailable
from .recurrence import next_occurrence, once_due, parse_event_start, relative_due
from .store import ScheduleStore

logger = logging.getLogger(__name__)

_DEFAULT_GRACE = datetime.timedelta(hours=2)


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class _Once:
    """Memoize a zero-arg call so per-user events are fetched at most once per
    tick, and only if a schedule actually needs them."""

    def __init__(self, fn: Callable[[], dict | None]):
        self._fn = fn
        self._done = False
        self._val: dict | None = None

    def get(self) -> dict | None:
        if not self._done:
            self._val = self._fn()
            self._done = True
        return self._val


def render_message(template: str, event: dict | None) -> str:
    """Substitute the known ``{event_*}`` tokens into a reminder template.

    Plain ``str.replace`` of a fixed token set — never ``str.format`` — because
    the template is user-authored: format-string access (``{0.__class__...}``)
    must not be reachable. Unknown/absent tokens render empty."""
    out = template or ""
    ev = event or {}
    for token, key in (("{event_title}", "title"), ("{event_time}", "time"),
                       ("{event_date}", "date")):
        if token in out:
            out = out.replace(token, str(ev.get(key, "")))
    return out


class Scheduler:
    def __init__(
        self,
        *,
        auth,
        schedules_dir: Callable[[int], str],
        run_agent: Callable[[str, int, str | None], None],
        send_message: Callable[[int, str], None],
        upcoming_events: Callable[[int], list],
        grace: datetime.timedelta = _DEFAULT_GRACE,
        tick_seconds: float = 30.0,
        clock: Callable[[], datetime.datetime] = _utcnow,
    ):
        self._auth = auth
        self._schedules_dir = schedules_dir
        self._run_agent = run_agent
        self._send_message = send_message
        self._upcoming_events = upcoming_events
        self._grace = grace
        self._tick_seconds = tick_seconds
        self._clock = clock
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ── lifecycle ───────────────────────────────────────────────────────────

    def start(self) -> None:
        """Spin up the daemon tick thread (idempotent)."""
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="scheduler", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick(self._clock())
            except Exception:
                logger.exception("scheduler tick failed")
            self._stop.wait(self._tick_seconds)

    # ── one pass ────────────────────────────────────────────────────────────

    def tick(self, now: datetime.datetime) -> None:
        """One full pass over every user. Public so tests can drive it directly
        with a fixed ``now`` instead of spinning the thread."""
        try:
            users = list(self._auth.list_users())
        except Exception:
            logger.exception("scheduler: list_users failed")
            return
        for urec in users:
            try:
                self._tick_user(urec.id, now)
            except Exception:
                logger.exception("scheduler: user %s tick failed", getattr(urec, "id", "?"))

    def _tick_user(self, user_id: int, now: datetime.datetime) -> None:
        try:
            dek = self._auth.get_dek(user_id)
        except BackgroundUnavailable:
            return  # pre-v2 account: skip silently until the user logs in once
        store = ScheduleStore(self._schedules_dir(user_id), dek)
        schedules = [s for s in store.list_schedules() if s.get("enabled")]
        if not schedules:
            return
        events = _Once(lambda: self._index_events(user_id))
        for sched in schedules:
            try:
                self._evaluate(sched, store, user_id, now, events)
            except Exception:
                logger.exception(
                    "scheduler: schedule %s failed", sched.get("schedule_id")
                )

    def _index_events(self, user_id: int) -> dict | None:
        """``{event_id: event}`` for the user, or ``None`` if the fetch failed.

        The ``None`` sentinel matters: a transient backend outage must NOT make
        every linked event look absent — that would delete live reminders as
        "orphans". On failure we skip relative schedules this tick and try again
        next time. (Genuine orphan cleanup relies on the fetch returning a
        horizon at least as far as the longest reminder lead — a wiring contract
        of ``upcoming_events``; see docs/scheduling.md §8.)"""
        try:
            evs = self._upcoming_events(user_id) or []
        except Exception:
            logger.exception("scheduler: upcoming_events failed for %s", user_id)
            return None
        return {e["id"]: e for e in evs if "id" in e}

    # ── per-schedule firing ──────────────────────────────────────────────────

    def _evaluate(self, sched, store, user_id, now, events) -> None:
        trigger = sched["trigger"]
        kind = trigger.get("kind")
        tz = sched.get("tz")
        state = sched.setdefault("state", {})

        if kind == "recurring":
            due = next_occurrence(trigger, after=state.get("last_run_at"), tz=tz)
            if due > now:
                return                                  # not yet
            if now - due > self._grace:                 # missed (downtime)
                state["last_run_at"] = due.isoformat()  # advance silently, no flood
                store.save(sched)
                return
            self._fire(sched, user_id, event=None)
            state["last_run_at"] = due.isoformat()      # scheduled instant, not now
            store.save(sched)

        elif kind == "once":
            if state.get("fired_at"):
                return
            due = once_due(trigger, tz)
            if due <= now <= due + self._grace:
                self._fire(sched, user_id, event=None)
                store.delete(sched["schedule_id"])      # one-shot: gone after firing

        elif kind == "relative":
            index = events.get()
            if index is None:
                return                                  # event fetch failed; don't touch it
            ev = index.get(trigger.get("event_id"))
            if ev is None:
                store.delete(sched["schedule_id"])      # orphan: linked event gone
                return
            start = parse_event_start(ev.get("date"), ev.get("time"), tz)
            start_iso = start.isoformat()
            if state.get("sent_for_start") == start_iso:
                return                                  # already sent for this start
            due = relative_due(start, trigger, tz)
            if due <= now <= start:                     # in the window, not past the event
                self._fire(sched, user_id, event=ev)
                state["sent_for_start"] = start_iso     # re-arms if the event moves
                store.save(sched)

    def _fire(self, sched, user_id, *, event) -> None:
        action = sched.get("action", {})
        kind = action.get("kind")
        if kind == "run_agent":
            self._run_agent(action.get("agent_id"), user_id, sched.get("tz"))
        elif kind == "send_message":
            self._send_message(user_id, render_message(action.get("message", ""), event))
        logger.info(
            "scheduler fired %s (%s) for user %s",
            sched.get("schedule_id"), kind, user_id,
        )
