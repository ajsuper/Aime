# Three-layer refactor plan

Goal: split the codebase into three layers so the Textual frontend can be
swapped without rewriting Aime's behavior.

```
provider_backend.py          (unchanged — provider/model I/O)
        ↑ AgentBackend protocol
src/aime/  (NEW — pure logic, no Textual)
        ↑ ConversationController + services + CoreEvent stream
src/tui_model.py             (shrunk — widgets, Rich markup, keybindings)
```

## What moves into `src/aime/`

| Module | Contents |
|---|---|
| `aime/config.py` | `API_URL`, `CONFIG_PATH`, `SYSTEM_PROMPT_PATH`, `SCHEMA_FILES`, `AGENT_MODEL`; system prompt loader. (Prefs stay in TUI — theme-only.) |
| `aime/tool_gateway.py` | `TOOL_NAME_MAP` + `ToolGateway.execute(name, payload) -> dict` (HTTP POST to local tool server). |
| `aime/services.py` | `CalendarService` (events for month / day), `TopicService` (list / get contents). Strips hardcoded year. |
| `aime/onboarding.py` | `SPECIAL_TOPICS`, `ONBOARDING_PROMPT`, `bootstrap_special_topics(gateway)`, `is_first_conversation(backend, gateway)`. |
| `aime/tool_formatting.py` | `format_tool_details`, `format_tool_response`, `_truncate_for_log`. Plain strings, no Rich markup. |
| `aime/controller.py` | `ConversationController` — owns the active `AgentBackend`, idle/busy state, pending-message queue, first-interaction flag, slash-command parsing (`/reset`, `/load`, `:q`, `/toggle_log_model_thinking`), stream pump, tool execution. Emits `CoreEvent` stream to subscribers. |
| `aime/replay.py` | `replay_messages(messages_snapshot) -> Iterator[CoreEvent]` — walks provider-native messages for `/load`. |

## `CoreEvent` kinds (the new seam)

```
user_message_shown, user_message_queued,
assistant_text, assistant_text_delta, assistant_text_end,
assistant_thinking,
tool_call, tool_result,
turn_end, ready, notice, session_restart, session_terminated, error
```

UI-agnostic plain dataclass. Tool details/results are pre-formatted as plain
strings; the UI applies its own markup.

## Controller API (frontend-facing)

- `start()` — spawn stream worker, kick off onboarding if first run.
- `dispatch_input(line) -> bool` — parse + handle a line of user input; returns `True` if app should quit.
- `send_user_message(text)` — bare message send (no slash parsing).
- `reset()` / `load(session_id)` — session ops; emit `session_restart` + replay.
- `subscribe(callback)` — register `CoreEvent` listener.
- `list_sessions()` — proxy to backend, for autocomplete.

Stream worker spawning is injected via a `worker_spawner` callable so the
controller doesn't depend on Textual's `run_worker`.

## What stays in `src/tui_model.py`

- Widgets: `AssistantView`, `CalendarView`, `TopicView`, `VimDataTable`, `VimTabs`, `CommandAutoComplete`, `Aime`.
- Rich markup + `_safe_write` + transcript rendering.
- Stream buffer / "thinking…" line / `_assistant_prefixed` (presentation state).
- Vim bindings, theme prefs (`load_prefs`/`save_prefs`).
- Subscribes to controller `CoreEvent` stream; `on_input_submitted` delegates to `controller.dispatch_input`.
- Calendar/Topic views call `CalendarService` / `TopicService` instead of raw `requests.post`.

## Migration order (5 steps, TUI works after each)

1. `aime/config.py` + `aime/tool_formatting.py` — pure moves.
2. `aime/tool_gateway.py` + `aime/services.py` — switch `CalendarView`/`TopicView`.
3. `aime/onboarding.py` — move first-conversation/bootstrap helpers.
4. `aime/controller.py` — introduce `CoreEvent`; `Aime` becomes subscriber.
5. `aime/replay.py` — replace `_replay_history` with `CoreEvent` walker.

## Decisions (confirmed)

- Layout: package `src/aime/` with submodules.
- CLI: architecture only — don't build a CLI driver yet.
- Prefs stay with TUI (`PREFS_PATH`, theme).
