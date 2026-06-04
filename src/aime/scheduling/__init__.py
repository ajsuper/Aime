"""The scheduler: one in-process loop that fires *scheduled things*.

A user keeps a single store of schedule records, each pairing a ``trigger``
(when) with an ``action`` (what) — see ``docs/scheduling.md``. The two launch
features ride on it: scheduled agents (``run_agent`` action) and event reminders
(``send_message`` action linked to an event). Future triggers add a source to
the loop without changing it.

Landing incrementally; today this package exposes the pure timing math
(``recurrence``). The store, the daemon-thread scheduler, and the source plumbing
arrive in subsequent steps.
"""

from .recurrence import (
    combine_local,
    next_occurrence,
    once_due,
    parse_event_start,
    relative_due,
)
from .store import (
    ScheduleStore,
    make_schedule,
    new_schedule_id,
    validate_schedule,
)
from .scheduler import Scheduler, render_message
from .reminders import ReminderService

__all__ = [
    "combine_local",
    "next_occurrence",
    "once_due",
    "parse_event_start",
    "relative_due",
    "ScheduleStore",
    "make_schedule",
    "new_schedule_id",
    "validate_schedule",
    "Scheduler",
    "render_message",
    "ReminderService",
]
