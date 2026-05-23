# Midnight Agent — design

A background framework for Aime: the side of the assistant that does things
while the user is away. Morning briefs over SMS, pre-event reminders, push
notifications the user can reply to and have it land as a real conversation
the next time they open the web app.

This document is a design sketch, not a built feature. Implementation is
staged; the first piece to land is the encryption story (see below) because
everything else depends on it.

---

## How it fits into the existing architecture

Aime is already layered for this:

- **C++ tool server** — per-`user_id` events/topics persistence. Already
  reachable from anywhere via `ToolGateway`.
- **`AgentBackend`** (`src/provider_backend.py`) — provider session abstraction.
  Sessions are first-class (`new_session`, `load_session`,
  `messages_snapshot`), so a user can have more than one — a foreground chat
  and a separate background session.
- **`ConversationController`** (`src/aime/controller.py`) — UI-agnostic. Emits
  `CoreEvent`s to any subscriber. `system_send_message` already exists as
  the "wake the model without a human typing" channel (onboarding uses it).
- **`UserContext`** (`src/frontends/web_app.py`) — per-user bundle: backend +
  gateway + controller + SSE fanout, cached in-process.

The midnight framework reuses all of this and adds a scheduler, a set of
triggers, an outbound channel abstraction, and an inbound webhook path.

---

## New components

Under `src/aime/midnight/`:

```
__init__.py      # public surface
service.py       # MidnightService — scheduler + dispatcher
triggers.py      # Trigger base + CronTrigger, CalendarTrigger, WebhookTrigger
jobs.py          # MidnightJob, JobKind enum, prompt templates
runner.py        # HeadlessRunner — drives one agent turn, returns text
channels.py      # Channel base + SMSChannel, PushChannel, InboxChannel
store.py         # SQLite next to auth.sql: schedules, job history, reply tokens
```

And prompts in `resources/prompts/midnight/` (`morning_brief.md`,
`pre_event.md`, ...).

Touchpoints in existing code:
- `web_app.py` — boot `MidnightService`, expose `/midnight/twilio` webhook,
  route inbound replies into the foreground controller.
- `aime/__init__.py` — lazy-export `MidnightService`.
- `aime/controller.py` — no change; already headless-capable.

### Trigger

Abstract source of "now is the time." Concrete kinds:

- `CronTrigger` — fixed time of day per user (e.g. 7am morning brief).
- `CalendarTrigger` — resolved against `CalendarService`, e.g. "30 minutes
  before each event today."
- `WebhookTrigger` — inbound (SMS reply, push reply).

Each fires `MidnightJob` records into a queue. One scheduler thread per
process walks a sorted heap; no third-party scheduler dependency.

### MidnightJob

Who (user_id), why (trigger kind), what prompt to inject, what channel to
deliver on. Example payload:

```python
{
    "user_id": 7,
    "kind": "morning_brief",
    "system_prompt": "It's 7am. Look at today's events and topics, "
                     "write a calm 2–3 sentence brief...",
    "channel": "sms",
}
```

### MidnightRunner

Per-job thread. Spins up a *secondary* `AgentBackend` session for the user
(kept separate from the foreground chat so notifications don't pollute the
visible transcript), wraps it in a lightweight headless controller, attaches
a `ChannelSink` subscriber that buffers `assistant_text` / `assistant_text_end`
into one message, submits a `system_send_message` with the trigger's prompt,
waits for `turn_end`, tears the session down.

### Channel

Outbound transport. One method, `deliver(user_id, text, reply_token)`.
`SMSChannel` (Twilio), `PushChannel`, `EmailChannel`, plus an `InboxChannel`
that writes to a file for tests.

### Inbound reply path

The reason the design earns its keep: when the user replies to an SMS,
Twilio webhooks `/midnight/twilio` on `web_app`. The handler:

1. Resolves `From` → `user_id`.
2. Looks up the cached foreground `UserContext` (constructing it if needed
   — see encryption section for how the DEK is available here).
3. Posts the SMS body as a regular `user_send_message` into the controller.

Net effect: when the user next opens the web app, the assistant's reply is
already waiting in the transcript and the conversation continues naturally.
No new agent code path — `dispatch_input` was already the seam.

---

## Process model

**Phase 1 (recommended starting point):** in-process. `MidnightService` is a
singleton owned by `web_app.py`, started at boot. Shares `_user_contexts`,
posts directly into existing controllers, Twilio webhooks are Flask routes.
Simple ops, easy testing.

**Phase 2 (later, if needed):** out-of-process daemon. `python -m
aime.midnight` runs separately, talks to the C++ backend via `ToolGateway`,
talks to `web_app` via an internal HTTP endpoint to post inbound messages.
Better isolation, survives web restarts.

---

## Design decisions worth pinning early

- **Two sessions per user, not one.** The morning brief lives in its own
  `AgentBackend` session so it doesn't shove itself into the middle of
  yesterday's foreground conversation. The only place the two merge is when
  an inbound SMS is routed into the *foreground* session — that's
  deliberate, so the user picks up where the assistant left off.
- **Read-only tools by default.** Unattended jobs get a reduced tool set.
  Write-capable tools (create event, edit topic) are opt-in per job kind.
- **Idempotency.** Triggers need a "last fired" record so a 6:59am process
  restart doesn't double-send the 7am brief. Lives in `midnight/store.py`.
- **Cost ceiling.** A self-driving loop needs a per-day budget per user.
  Wire into `aime/usage.py`.
- **Tone.** Outbound messages follow [creativity-theme] and the
  friendly-error-messaging guidance — calm, non-startling.

---

## Encryption — done

The encryption groundwork is in place: the at-rest scheme has been moved
from a password-derived KEK to a machine-secret-derived KEK, so every
user's DEK is reachable on the host without their password. See the
[At-rest encryption](./security.md#at-rest-encryption) section of
`security.md` for the full design.

Net effect for the midnight framework:

- `MidnightService` calls `auth_backend.get_dek(user_id)` and gets the
  DEK back. No opt-in step, no per-user setup — every active account is
  reachable by default.
- The threat-model trade-off ("a full host compromise can decrypt every
  user's data") is now the explicit default. Documented in
  [security.md](./security.md#3-the-server-can-decrypt-any-users-data),
  not hidden behind a setting.
- Pre-v2 accounts (early-beta) raise `BackgroundUnavailable` until the
  user logs in once and the row is auto-upgraded. The midnight service
  skips them silently in the meantime.

---

## Implementation order

1. ~~**Encryption: machine-secret-derived KEK.**~~ ✅ Landed.
2. `MidnightService` skeleton + `CronTrigger` + `InboxChannel`. End-to-end:
   "at HH:MM, run this prompt for this user, write the result to a file."
   (← starting here.)
3. `HeadlessRunner` proper, sharing `ConversationController`.
4. `CalendarTrigger`.
5. `SMSChannel` + inbound webhook + reply routing into foreground session.
6. Usage budget + idempotency hardening.
