# Background agents

A small framework for running **headless workers** that reuse the full Aime
stack — the same model, tools, and database access the chat assistant has — to
carry out one task against a user's data and return a result.

A background agent is *Aime with a job and no chat window*. You hand it a task,
it works autonomously against the user's events/topics (and the web, if
allowed), and it comes back with a structured answer.

This is the task-runner layer. It does **not** decide *when* agents run — that
is the job of a scheduler/trigger layer (see `midnight-agent.md`), which calls
`BackgroundAgentRunner.run(...)` like any other caller. This first cut is
library-API-only: no scheduling, no web endpoint, no model-invoked spawning.

The framework ships with **no concrete agents** — only the machinery to build
them.

## How it fits the existing architecture

Nothing here is new infrastructure; it is the interactive stack wired in a
headless configuration:

- **`ToolGateway(user_id=…)`** — the database access. Every tool call routes
  through the C++ tool server to that user's database. Hand a worker a gateway
  bound to a user id and it can read and write that user's events and topics.
- **`AnthropicMessagesBackend`** — the provider/agent loop, configured with the
  headless base prompt, the agent's `SubmitResult` terminal tool, and
  `persist_enabled=False` (the conversation is in-memory only).
- **`ConversationController(headless=True)`** — drives the loop and dispatches
  tools. Headless mode skips onboarding and arms `SubmitResult`; the whole tool
  path (web search offload, commitment tools, result formatting) is reused
  unchanged, so agents inherit all of it for free.
- **`system_send_message`** — the "wake the model without a human typing"
  channel onboarding already uses. The task brief is delivered this way.

## The pieces (`src/aime/agents/`)

```
spec.py       AgentSpec   — the definition of an agent (the public surface)
              AgentResult — what a run returns
runner.py     BackgroundAgentRunner — builds the stack, runs it, persists the run
collector.py  ResultCollector — CoreEvent subscriber → result (headless "frontend")
store.py      AgentRunStore — encrypted, per-user persistence of runs
registry.py   name → AgentSpec lookup
```

Supporting resources:

- `resources/prompts/agents/_base.md` — the headless base system prompt: *you
  are a worker, there is no user to ask, finish by calling SubmitResult.*
- `resources/tools/api_submit_result_schema.json` — the `SubmitResult` terminal
  tool. A run ends when the worker calls it; its `summary` and optional
  `result` are what the caller gets back.

## Lifecycle of a run

1. The runner builds a backend (base prompt + per-agent `SubmitResult`, no
   persistence), a `ToolGateway` bound to `user_id`, and a headless controller
   with a `ResultCollector` subscribed.
2. `controller.start()` arms `SubmitResult`; the runner submits the task as a
   `system_send_message`.
3. The model works the task with its tools (against the user's DB), then calls
   `SubmitResult`. The controller surfaces that as an `agent_result` CoreEvent
   and deliberately sends **no** tool response — there is nothing left to do.
4. The collector resolves; the runner tears the session down, builds an
   `AgentResult`, and writes an encrypted run record via `AgentRunStore`.

If a worker ends a reply round without submitting, the runner nudges it back
toward `SubmitResult` a bounded number of times, then gives up with a
`no_result` / `max_turns` status. A worker that stops responding times out.

## Persistence

Runs are kept **separate** from conversations: a background agent never appears
in the chat history or `/load` list. Each run is written to
`users/<id>/agent_runs/<run_id>.json.enc`, encrypted with the user's DEK (same
protection as conversations, run id bound in as the AEAD AAD). The record holds
inputs, status, summary, structured result, timing, and the full transcript —
an auditable trail of what every agent did. Persistence is best-effort: a failed
write never breaks the run, whose result is still returned in memory.

### Viewing runs

The web frontend surfaces these records to **Verbose** users only (the same
tier that already sees per-turn tool chatter), folded into the Conversations
menu beneath the user's chats. Calmer tiers never fetch them and never see the
section. Two read-only routes back it: `GET /agent-runs` (decrypted metadata,
newest first) and `GET /agent-runs/<run_id>` (the full record). Clicking a run
opens a modal showing its status, summary, structured result, and a collapsible
transcript — runs aren't conversations, so this never touches the live chat
session or the `/load` path.

### Running one ad-hoc (Advanced)

**Advanced** users (the UI-mode toggle, distinct from Verbose) get a dedicated
**Agents** pane in the left rail. It's a transient launcher, not a manager:
type a system message, optionally allow web search, and hit *Run agent*. That
posts to `POST /agents/run`, which builds a one-off `AgentSpec`, registers it
in the in-memory registry under a unique `adhoc-…` name, and runs it on a
daemon thread bound to the user's id/DEK. The agent *definition* is not
persisted — it's gone on the next server restart — only the encrypted run
record survives, and it shows up in the same pane's runs list (and the Verbose
conversations section) when the run finishes. A `agent_run_update` SSE ping
refreshes open panes when a run starts and finishes. This is still the only
in-app path that calls `BackgroundAgentRunner.run(...)`; there is no scheduler
or persistent agent collection yet.

## Building an agent

Declare a spec and register it — no plumbing per agent:

```python
from aime.agents import AgentSpec, register

register(AgentSpec(
    name="calendar-auditor",
    description="Flags events that look stale or mislabeled.",
    instructions=(
        "Review this user's events for {month}. Flag any that look stale, "
        "duplicated, or mis-categorized, with a one-line reason each."
    ),
    result_schema={
        "type": "object",
        "properties": {
            "flagged": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "event_id": {"type": "integer"},
                        "reason": {"type": "string"},
                    },
                },
            }
        },
    },
    # tool_allowlist=frozenset({"FilterUsersEvents", "GetTopicContents"}),  # read-only
    # max_turns=10, allow_web_search=False,
))
```

Run it:

```python
from aime.agents import BackgroundAgentRunner, get

runner = BackgroundAgentRunner()
result = runner.run(
    get("calendar-auditor"),
    inputs={"month": "June 2026"},
    user_id=1,
    dek=dek,                       # the user's data key
    runs_dir=".../users/1/agent_runs",
    usage_label="alice",           # usage attribution (optional)
)

if result.ok:
    print(result.summary_text)     # SubmitResult summary
    print(result.result)           # structured payload, shape per result_schema
```

`AgentSpec` knobs: `result_schema` (shape of the structured result),
`tool_allowlist` (restrict tools, e.g. read-only agents), `model`, `max_turns`,
`allow_web_search`.

## Notes & deliberate choices

- **No model routing.** A run executes every turn on `spec.model` so its
  behavior and cost are predictable, rather than being re-routed per turn.
- **`SubmitResult` is the only way to return a value.** Anything not put in it
  is lost; the base prompt makes this explicit to the model.
- **The terminal-tool slot is shared with onboarding.** `CompleteOnboarding`
  (interactive) and `SubmitResult` (agents) use the same backend mechanism
  (`set_terminal_tool_active`, appended after the cache breakpoint so toggling
  never busts the cached tool prefix).
