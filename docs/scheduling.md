# Scheduling — implementation plan

One in-process scheduler that fires **scheduled things**: a single per-user
store of records, each of which says *when* to trigger and *what* to do. It
covers two user-facing features at launch — **scheduled agents** ("run this
agent every weekday at 7am") and **event reminders** ("text me 8am three days
before my trip") — and is built so future triggers (midnight digest, commitment
follow-ups) plug in without touching the loop.

This supersedes the event-metadata approach sketched earlier: reminders are
**not** stored on the event row, so there is **no C++/`serve.cpp` schema change**.
A reminder is a schedule record that *links* to an event by id and recomputes
its fire time against the live event every tick — so moving the event moves the
reminder for free, with no reconciliation.

Builds on [[background-agents-framework]] and the broader [[midnight-agent]]
vision; this is the concrete first slice of it.

---

## 1. The unified schedule record

One record shape, with every field living inside the variant that uses it —
illegal states are unrepresentable, a reader just switches on the `kind`s.

```jsonc
{
  "schedule_id": "sch-<slug>-<rand>",
  "enabled": true,
  "tz": "America/New_York",          // user-local tz the trigger is interpreted in

  "trigger": {
    "kind": "recurring",             // recurring | once | relative
    // -- recurring (cron-like):
    "freq": "weekly",                //   daily | weekly | monthly
    "time": "07:00",                 //   HH:MM, user-local
    "weekday": 1,                    //   1-7 (Mon-Sun), weekly only
    "day": null,                     //   1-31, clamped to month end, monthly only
    // -- once:
    "at": "2026-06-10T15:00:00",     //   absolute, user-local
    // -- relative (anchored to a linked event):
    "event_id": 42,                  //   the event this fires relative to
    "days_before": 3,                //   shift the event's DATE back N days (0 = event's own day)
    "at_time": "08:00",              //   wall-clock on that day; null = inherit event's start time
    "minutes_before": 0              //   fine delta subtracted from the above (used when at_time is null)
  },

  "action": {
    "kind": "send_message",          // send_message | run_agent
    // -- send_message:
    "message": "Reminder: {event_title} at {event_time}",
    // -- run_agent:
    "agent_id": "agent-morning-brief-9f8e"   // references a saved AgentDefinition
  },

  "state": {
    "last_run_at": null,             // recurring: advances to the scheduled instant on each fire
    "fired_at": null,                // once: set when fired (record then deleted)
    "sent_for_start": null           // relative: the event's start-iso at last fire (dedup + re-arm)
  },

  "created_at": "...", "updated_at": "..."
}
```

**Invariants enforced at `save()`** (reject otherwise, best-effort → return False):
- `trigger.kind == "relative"` ⟺ `trigger.event_id` is set.
- `recurring`: `freq` ∈ {daily,weekly,monthly}; `weekday` required iff weekly; `day` required iff monthly.
- `relative`: `0 ≤ days_before ≤ 366`; `at_time` matches `HH:MM` or is null.
- `action.kind == "run_agent"` ⟺ `agent_id` set; `send_message` ⟺ `message` set.

---

## 2. Storage — `src/aime/scheduling/store.py`

`ScheduleStore`, a near-exact clone of `AgentDefinitionStore`
(`src/aime/agents/definitions.py`):

- One encrypted file per record: `users/<id>/schedules/<schedule_id>.json.enc`.
- Sealed with the user's DEK via `encryption.encrypt_blob`, `schedule_id` bound
  in as AEAD associated data.
- All IO best-effort: a failed write returns `False`, an unreadable file is
  skipped in listings — a storage hiccup never takes down request handling.
- Methods: `save(record)`, `load(id)`, `list_schedules()`, `delete(id)`,
  plus helpers `make_schedule(...)` / `new_schedule_id(name)` mirroring
  `make_definition` / `new_agent_id`.

Keeping schedules beside agents/runs means a user's whole footprint stays inside
their one directory; nothing lands in a shared SQL table.

---

## 3. Recurrence math — `src/aime/scheduling/recurrence.py`

**Pure functions, no IO, fully unit-testable.** All arithmetic lives here so
neither the model nor the loop ever does date math.

```python
def next_occurrence(trigger: dict, *, after: str | None, tz: str) -> datetime:
    """Next recurring fire strictly after `after` (or now if None), in `tz`.
    Handles daily/weekly/monthly, day-of-month clamping, and DST via zoneinfo."""

def once_due(trigger: dict, tz: str) -> datetime: ...

def relative_due(event_start: datetime, trigger: dict, tz: str) -> datetime:
    base_date = event_start.date() - timedelta(days=trigger["days_before"])
    base_time = (time.fromisoformat(trigger["at_time"])
                 if trigger.get("at_time") else event_start.timetz())
    due = combine_local(base_date, base_time, tz)        # tz/DST-correct
    return due - timedelta(minutes=trigger.get("minutes_before", 0))
```

`combine_local` localizes a naive date+time into `tz` so "08:00" is 08:00 local
across DST boundaries.

---

## 4. The scheduler core — `src/aime/scheduling/scheduler.py`

A daemon thread, single instance per process. The deployment is single-process
and threaded (`exec python -m frontends.web_app`, `docker-entrypoint.sh:71`), so
one thread owns it and **nothing double-fires**.

**Dependency-injected** so the core stays in `src/aime/` with no import of
`frontends/`. `web_app` constructs it with callables:

```python
Scheduler(
    auth_backend,                       # for list_users / get_dek / lookup
    schedules_dir_for,                  # user_id -> path
    run_agent=...,                       # (agent_id, user_id, tz) -> None   [_launch_agent_run]
    send_message=...,                    # (user_id, text) -> None           [messenger + contact]
    upcoming_events=...,                 # (user_id) -> list[event dict]      [ToolGateway.get_events]
    load_agent=...,                      # (user_id, dek, agent_id) -> definition
    grace=timedelta(hours=2),
    tick_seconds=30,
)
```

```python
def _loop(self):
    while not self._stop.is_set():
        try:    self.tick(now=utcnow())
        except Exception: pass               # one bad user never kills the loop
        self._stop.wait(self.tick_seconds)

def tick(self, now):
    for rec in self._auth.list_users():
        try:    dek = self._auth.get_dek(rec.user_id)
        except BackgroundUnavailable: continue   # pre-v2 account: skip until login
        store = ScheduleStore(self._dir(rec.user_id), dek)
        events = None   # lazy-loaded only if a relative record needs it
        for sched in store.list_schedules():
            if not sched["enabled"]: continue
            self._evaluate(sched, store, rec, dek, now, events_loader)
```

### Firing logic, per `trigger.kind`

```python
recurring:
    due = next_occurrence(t, after=state["last_run_at"], tz)
    if due > now:                 pass                      # not yet
    elif now - due > grace:       state["last_run_at"] = due; store.save(sched)   # missed → advance, no flood
    else:                         fire(); state["last_run_at"] = due; store.save(sched)

once:
    if state["fired_at"]: continue
    due = once_due(t, tz)
    if due <= now <= due + grace: fire(); store.delete(sched["schedule_id"])      # one-shot, gone after firing

relative:
    ev = events.get(t["event_id"])
    if ev is None:                store.delete(sched["schedule_id"])              # orphan: event deleted → auto-clean
    else:
        due = relative_due(ev.start, t, tz)
        if state["sent_for_start"] != ev.start_iso and due <= now <= ev.start:
            fire(); state["sent_for_start"] = ev.start_iso; store.save(sched)     # re-arms automatically if event moves
```

Notes:
- **`last_run_at` is set to `due`, not `now`** — keeps recurring fires on the
  grid instead of drifting by the tick's lateness.
- **No `next_run_at` is ever stored.** Every fire decision is recomputed each
  tick from durable state (`last_run_at` / `fired_at` / `sent_for_start`), so a
  mid-tick crash re-derives identically on reboot. **Restart-safe by design.**
- **Grace window (2h)** stops a machine that was off 6am–noon from firing six
  missed dailies at once.

### Dispatch (`fire`)

Switches on `action.kind`:
- `send_message` → render the template (`{event_title}`, `{event_time}`, … from
  the linked event when present) → `send_message(user_id, text)`, which resolves
  the user's `messaging_contact` (`auth.lookup`) and calls
  `get_messenger().send(contact, text)`. **Zero tokens.** No contact / messaging
  disabled → skip quietly (don't mark fired, so it can deliver once configured —
  or mark fired to avoid retries; see Open questions).
- `run_agent` → `load_agent(agent_id)` → `_launch_agent_run(definition_to_spec(d),
  user_id, tz, agent_id=...)`. Byte-for-byte identical to clicking **Run** on the
  card: same `BackgroundAgentRunner`, same encrypted run record under
  `agent_runs/`, same "running…" fanout.

---

## 5. Sources — `src/aime/scheduling/sources.py`

At launch the store *is* the one source, so the tick reads `ScheduleStore`
directly. The `Source` protocol (`due_items(user_id, dek, now) -> Iterable[DueItem]`)
is kept as the extension seam: future **derived** triggers (commitment
follow-ups, midnight digest) that aren't user-created records add a source to
the list without changing the loop. We ship one store-backed source now and
grow the list later.

---

## 6. Web-app integration — `src/frontends/web_app.py`

### Boot
A module-level `_scheduler` singleton, started from the `__main__` block (and
any other entrypoint) via `_start_scheduler()`, guarded by `AIME_SCHEDULER`
(default on; set `0` in tests). Constructed with the injected callables above,
reusing existing helpers: `_launch_agent_run` (`web_app.py:1968`),
`get_messenger()`, `_auth_backend`, a thin `ToolGateway(user_id).call("get_events", ...)`.

### Routes (mirror the existing `/agents` CRUD at `web_app.py:2093`+)
```
GET    /schedules                 list this user's schedule records
POST   /schedules                 create (validated against the invariants)
PUT    /schedules/<id>            update
DELETE /schedules/<id>            delete
POST   /schedules/<id>/run        fire now (manual test trigger)
```
Each handler builds `ScheduleStore(_schedules_dir(user_id), get_dek(user_id))` —
same pattern as `_agent_store` / `_run_store` at `web_app.py:1927`.

### Agent definition cleanup
Remove the inert `schedule` string from `make_definition` /
`definition_to_spec` (`agents/definitions.py:56`) and the `/agents` handlers
(`web_app.py:2138,2170`). An agent's schedule now lives as a `run_agent`
schedule record referencing its `agent_id` — so an agent can have zero or
(later) several schedules, and the two concerns stop bleeding into one field.

---

## 7. Frontend — `resources/style/web_chat.html`

### Scheduled agents
Replace the dead **Schedule (coming soon)** text input (line ~2322) in the
agent edit modal with a real control:
- a **Daily / Weekly / Monthly** segmented control + a **time** picker,
- a conditional **weekday** picker (weekly) or **day-of-month** picker (monthly),
- an **enabled** toggle (also surfaced on the agent card).

On save, the modal writes a `run_agent` schedule record via `/schedules`
(creating, updating, or deleting it alongside the agent). The card shows a small
"next run" hint computed client-side from the same fields.

### Event reminders
In the event modal, a **Reminders** section: a list (multiple per event) where
each row is `days_before` + an optional wall-clock `at_time` (blank = inherit
start) — rendering the user's sentence ("8:00am, 3 days before"). Each row is a
`relative` + `send_message` schedule record linked by `event_id`. Adding/removing
rows calls `/schedules`. Because the record only *links* to the event, deleting
the reminder never touches the event, and moving the event needs no client work
at all — the next tick recomputes.

---

## 8. Backend (C++) — no change

`get_events` already returns each event's `id`, date, and time
(`serve.cpp:1075`), which is everything the `relative` evaluator needs. No new
column, no recompile. The only requirement is that `upcoming_events(user_id)`
returns enough horizon (e.g. next ~400 days, matching the `days_before` cap) for
far-out reminders to be seen before they're due.

---

## 9. Implementation order

1. **`recurrence.py` + unit tests.** Pure math, no dependencies — `next_occurrence`
   (daily/weekly/monthly, DST, month-end clamp), `once_due`, `relative_due`.
   Lock the semantics here first.
2. **`store.py` + tests.** Clone `AgentDefinitionStore`; round-trip encrypt,
   list ordering, validation/invariant rejection.
3. **`scheduler.py` core + tests.** Drive `tick()` with injected fakes (fake
   auth, in-memory store, recording `run_agent`/`send_message`, canned events).
   Cover: fire-once, grace skip, missed→advance, relative re-arm on moved event,
   orphan cleanup, `once` self-delete.
4. **Wire into `web_app`**: `_start_scheduler()` in `__main__`, the `/schedules`
   CRUD routes, `_schedules_dir`, the injected callables. Verify a `run_agent`
   schedule fires an actual run end-to-end.
5. **Frontend — scheduled agents**: real schedule control in the agent modal +
   card hint/toggle. Remove the dead field and the definition `schedule` string.
6. **Frontend — event reminders**: Reminders section in the event modal; verify
   a `send_message` reminder lands via the messenger and re-arms after a reschedule.
7. **Hardening**: `AIME_SCHEDULER` guard, per-day cost ceiling for `run_agent`
   schedules (wire into `aime/usage.py`, per [[midnight-agent]]), structured
   logging of fires/skips.

---

## 10. Open questions (decide during build, not blocking)

1. **Undeliverable `send_message`** (no contact / messaging off): skip-and-retry
   each tick, or mark fired so it doesn't nag? Leaning *mark fired* with a
   logged warning — a reminder that's hours stale isn't worth delivering late.
2. **Recurring event reminders.** A `relative` record links one event instance;
   recurring events are separate rows sharing a `commitmentId` (`serve.cpp:53`).
   "30 min before *every* standup" = a later enhancement (link by `commitmentId`,
   dedup per instance). Out of scope for v1.
3. **After-the-event follow-ups.** Trivial extension to `relative` (signed
   offset / `anchor: after`); nobody's asked yet, left out.
4. **Cost ceiling granularity** for scheduled agent runs — per-user/day budget
   shape, shared with the midnight vision.
</content>
</invoke>
