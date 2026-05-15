"""Provider-agnostic agent backend.

Decoupled from any UI so it can be reused by alternate front-ends. The UI
only ever talks to an `AgentBackend`; concrete implementations live below.
"""

import json
import os
import hashlib
import threading
import datetime
from dataclasses import dataclass
from typing import Iterator, Literal, Protocol, runtime_checkable

from anthropic import Anthropic

DATABSE_DIR = os.environ['HOME'] + "/.local/share/aime-assistant/"
CONVERSATIONS_DIR = os.path.join(DATABSE_DIR, "conversations")


def _jsonable(obj):
    """Recursively coerce `obj` into something json.dump can handle.

    Provider SDKs hand back pydantic models (e.g. WebSearchResultBlock) that
    are not JSON serializable. Anything we can't convert cleanly is stringified
    as a last resort so a single odd block can never poison the message list.
    """
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    # pydantic v2 / v1 models
    for attr in ("model_dump", "dict"):
        method = getattr(obj, attr, None)
        if callable(method):
            try:
                return _jsonable(method())
            except Exception:
                pass
    try:
        json.dumps(obj)
        return obj
    except (TypeError, ValueError):
        return str(obj)


# ============================================================================
# Provider-agnostic agent backend
# ============================================================================
#
# The UI only ever talks to a `AgentBackend`. Anthropic-specific types,
# session lifecycle, and event-stream parsing live behind this interface so
# swapping to a different provider (Anthropic Messages API, OpenAI,
# self-hosted, etc.) only requires writing a new concrete backend class.
#
#   UI ──submit(BackendEvent)──> Backend ──provider API──> model
#   UI <──stream() yields ──── Backend <──provider events
#

EventKind = Literal[
    # UI → backend (passed to submit)
    "user_send_message",
    "system_send_message",
    "tool_send_response",
    # backend → UI (yielded from stream)
    "assistant_send_text",
    "assistant_text_delta",
    "assistant_text_end",
    "assistant_thinking",
    "assistant_use_tool",
    "turn_end",
    "error",
    "session_terminated",
]


@dataclass
class BackendEvent:
    kind: EventKind
    text: str | None = None
    tool_name: str | None = None
    tool_input: dict | None = None
    tool_use_id: str | None = None
    tool_result: dict | None = None
    # If True (default), the UI is expected to execute the tool locally and
    # submit a `tool_send_response` event back. Server-side tools handled
    # entirely by the provider set this to False (display only).
    expects_response: bool = True
    stop_reason: str | None = None
    error: str | None = None


@dataclass
class SessionInfo:
    """A resumable conversation, as surfaced to session pickers in the UI.

    Deliberately provider-neutral: just enough to display the conversation in
    a list and pass `id` back to `load_session()`.
    """
    id: str
    summary: str = ""
    saved_at: str = ""


@runtime_checkable
class AgentBackend(Protocol):
    """Provider-agnostic interface for an agentic conversation backend."""

    def new_session(self) -> str:
        """Start a fresh session. Returns the session id."""
        ...

    def list_sessions(self) -> list[SessionInfo]:
        """Return resumable sessions, most-recently-saved first. Backends with
        no enumerable history return an empty list."""
        ...

    def load_session(self, session_id: str) -> None:
        """Resume a previously-started session by id."""
        ...

    def messages_snapshot(self) -> list[dict]:
        """Provider-native message dicts for the active session, in order.
        Used by the UI to replay history into the transcript on /load.
        Backends without an enumerable history return an empty list."""
        ...

    def reset(self) -> None:
        """Terminate the current session and start a new one."""
        ...

    def submit(self, event: BackendEvent) -> None:
        """Push a user/system/tool-result event into the conversation."""
        ...

    def stream(self) -> Iterator[BackendEvent]:
        """Yield normalized events from the model. Blocks until the session
        terminates; meant to run on a worker thread."""
        ...

    def shutdown(self) -> None:
        """Clean up any provider resources."""
        ...

class AnthropicMessagesBackend:
    """Anthropic Messages API implementation of AgentBackend.

    Maintains the conversation history client-side as a list of message dicts
    and drives the agent loop by calling messages.stream() once per assistant
    turn. Tool uses are surfaced to the UI, which executes them locally and
    submits results back via tool_send_response; once every tool_use in the
    last assistant turn has a matching result, the loop continues.
    """

    # --- compaction tuning ---
    # Once the history grows past this many messages, the oldest ones are
    # folded into a single summary message at the start of the next turn.
    COMPACT_TRIGGER_MSGS = 15
    # How many of the oldest messages to fold into the summary per compaction
    # pass. The cut point is nudged forward from here to land on a safe
    # boundary (see _maybe_compact).
    COMPACT_BATCH_MSGS = 3
    # Cheap model used for the summarization call.
    COMPACT_MODEL = "claude-haiku-4-5-20251001"
    # Prefix that marks a message as a compaction summary (so later passes can
    # detect and merge it instead of re-summarizing from scratch).
    _SUMMARY_MARKER = "[Conversation summary so far]"

    def __init__(
        self,
        system_prompt: str,
        model: str,
        schema_files: list[str],
        max_tokens: int = 8192,
    ):
        self._client = Anthropic(max_retries=3)
        self._system_prompt = system_prompt
        self._model = model
        self._schema_files = schema_files
        self._max_tokens = max_tokens
        self._tools = [
            {"type": "web_search_20250305", "name": "web_search"},
            *[self._load_schema(p) for p in schema_files],
        ]
        # The system prompt and tool schemas are byte-identical on every turn,
        # so mark them as prompt-cache breakpoints. After the first call they're
        # served from cache (~0.1x input cost) instead of re-billed in full.
        self._system = [
            {
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }
        ]
        if self._tools:
            self._tools[-1] = {
                **self._tools[-1],
                "cache_control": {"type": "ephemeral"},
            }

        self._session_id: str | None = None
        # One-sentence human-readable description of the session, shown in
        # session pickers. Generated by Haiku from the user's first prompt and
        # refreshed during compaction. Persisted in the session file.
        self._summary: str = ""
        # True while a background _generate_title thread is in flight, so
        # submit() doesn't spawn a duplicate one on the next message.
        self._title_generating = False
        self._messages: list[dict] = []
        self._pending_tool_results: list[dict] = []
        self._expected_tool_use_ids: set[str] = set()
        self._turn_trigger = threading.Event()
        self._terminated = threading.Event()
        # Bumped on every reset(). A stream loop captures the epoch when it
        # starts; once it no longer matches, that loop is stale and must exit.
        # This is what lets reset() re-arm _terminated/_turn_trigger for the
        # *new* stream loop without a still-running old loop mistaking the
        # re-armed (cleared) state for "keep going".
        self._epoch = 0
        self._lock = threading.Lock()
        # Serializes the whole _persist() operation. The turn loop and the
        # background title thread both persist concurrently; without this they
        # race on the temp file and can clobber a freshly-written title with a
        # stale snapshot.
        self._persist_lock = threading.Lock()

    # --- AgentBackend interface ---

    def new_session(self) -> str:
        with self._lock:
            self._messages = []
            self._summary = ""
            self._title_generating = False
            self._pending_tool_results = []
            self._expected_tool_use_ids = set()
        self._terminated.clear()
        self._turn_trigger.clear()
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        self._session_id = f"msgs-{stamp}-" + hashlib.sha1(os.urandom(8)).hexdigest()[:8]
        # Don't persist yet: the session is empty. The first submit() will
        # write the file once the user has actually said something, so that
        # /reset (or app launch) doesn't litter conversations/ with empty
        # placeholders.
        return self._session_id

    def messages_snapshot(self) -> list[dict]:
        """Copy of the current message list, safe for the UI to iterate
        without holding the backend lock."""
        with self._lock:
            return list(self._messages)

    def load_session(self, session_id: str) -> None:
        path = self._session_path(session_id)
        with open(path) as f:
            data = json.load(f)
        # Same epoch-bump dance as reset(): the previous stream worker thread
        # (Textual can't actually kill thread workers — exclusive=True only
        # flags them) must observe its captured epoch is stale and exit, or
        # we'd have two stream loops sharing self._messages and answering
        # every prompt twice.
        self._terminate_active_stream()
        with self._lock:
            self._session_id = session_id
            self._messages = data.get("messages", [])
            saved_summary = data.get("summary", "")
            self._summary = "" if saved_summary in ("", "none") else saved_summary
            self._title_generating = False
            self._pending_tool_results = []
            self._expected_tool_use_ids = set()
        self._terminated.clear()
        self._turn_trigger.clear()

    @staticmethod
    def _session_path(session_id: str) -> str:
        os.makedirs(CONVERSATIONS_DIR, exist_ok=True)
        return os.path.join(CONVERSATIONS_DIR, f"{session_id}.json")

    def list_sessions(self) -> list[SessionInfo]:
        # History lives as one JSON file per session on disk; enumerate them
        # and surface just the id + summary the UI needs. A file we can't read
        # or parse is skipped rather than failing the whole listing.
        sessions: list[SessionInfo] = []
        try:
            names = os.listdir(CONVERSATIONS_DIR)
        except OSError:
            return []
        for name in names:
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(CONVERSATIONS_DIR, name)) as f:
                    data = json.load(f)
            except (OSError, ValueError):
                continue
            summary = data.get("summary", "")
            if summary == "none":
                summary = ""
            sessions.append(SessionInfo(
                id=data.get("id") or name[:-len(".json")],
                summary=summary,
                saved_at=data.get("saved_at", ""),
            ))
        sessions.sort(key=lambda s: s.saved_at, reverse=True)
        return sessions

    def _persist(self) -> None:
        if not self._session_id:
            return
        # Hold _persist_lock across the snapshot *and* the file write, so a
        # slow writer can't os.replace() a stale snapshot over a newer one
        # (e.g. overwriting the title with "none"). The temp file name is made
        # unique per call as a second line of defense against collisions.
        with self._persist_lock:
            try:
                path = self._session_path(self._session_id)
                with self._lock:
                    snapshot = {
                        "id": self._session_id,
                        "saved_at": datetime.datetime.now().isoformat(timespec="seconds"),
                        "summary": self._summary or "none",
                        "messages": list(self._messages),
                    }
                tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
                with open(tmp, "w") as f:
                    json.dump(snapshot, f, default=_jsonable)
                os.replace(tmp, path)
            except (OSError, TypeError, ValueError):
                # OSError: disk/path issues. TypeError/ValueError: a content
                # block slipped through that even `default=_jsonable` couldn't
                # coerce — drop the write rather than crash the turn.
                pass

    def _terminate_active_stream(self) -> None:
        # Wake the current stream loop and force it to observe its captured
        # epoch is stale, so it exits with session_terminated before we mutate
        # the shared session state. Used by reset() and load_session() — both
        # swap the conversation out from under any in-flight stream worker.
        with self._lock:
            self._epoch += 1
        self._terminated.set()
        self._turn_trigger.set()

    def reset(self) -> None:
        self._terminate_active_stream()
        self.new_session()

    def shutdown(self) -> None:
        self._terminated.set()
        self._turn_trigger.set()

    def submit(self, event: BackendEvent) -> None:
        if event.kind in ("user_send_message", "system_send_message"):
            with self._lock:
                self._messages.append({
                    "role": "user",
                    "content": [{"type": "text", "text": event.text or ""}],
                })
                # Generate the session description as soon as there is at
                # least one user message and the session has no title yet
                # (covers fresh sessions and ones resumed from a persisted
                # "none"). _title_generating guards against spawning a second
                # Haiku thread while one is already in flight.
                user_texts = [
                    block["text"]
                    for msg in self._messages
                    if msg["role"] == "user"
                    for block in msg["content"]
                    if isinstance(block, dict) and block.get("type") == "text"
                ]
                needs_title = (
                    not self._summary
                    and len(user_texts) >= 1
                    and not self._title_generating
                )
                if needs_title:
                    self._title_generating = True
                title_prompt = "\n\n".join(user_texts)
            self._persist()
            # Off the turn thread so the Haiku call never delays the response.
            if needs_title and event.kind == "user_send_message":
                threading.Thread(
                    target=self._generate_title,
                    args=(title_prompt,),
                    daemon=True,
                ).start()
            self._turn_trigger.set()
        elif event.kind == "tool_send_response":
            result = event.tool_result
            if isinstance(result, (dict, list)):
                content_text = json.dumps(result)
            else:
                content_text = str(result if result is not None else "")
            with self._lock:
                self._pending_tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": event.tool_use_id,
                    "content": content_text,
                })
                self._expected_tool_use_ids.discard(event.tool_use_id)
                ready = not self._expected_tool_use_ids
                if ready:
                    self._messages.append({
                        "role": "user",
                        "content": self._pending_tool_results,
                    })
                    self._pending_tool_results = []
            if ready:
                self._persist()
                self._turn_trigger.set()
        else:
            raise ValueError(f"submit() does not accept event kind: {event.kind}")

    def stream(self) -> Iterator[BackendEvent]:
        with self._lock:
            my_epoch = self._epoch
        while True:
            self._turn_trigger.wait()
            # Check staleness/termination *before* clearing the trigger: a
            # stale loop must leave the trigger intact so the fresh loop still
            # sees the wake-up meant for it.
            with self._lock:
                stale = self._epoch != my_epoch
            if stale or self._terminated.is_set():
                yield BackendEvent(kind="session_terminated")
                return
            self._turn_trigger.clear()
            # The trigger can be left set transiently across a reset; if there
            # is nothing queued there is no turn to run, so just wait again.
            with self._lock:
                has_messages = bool(self._messages)
            if not has_messages:
                continue
            try:
                yield from self._run_turn()
            except Exception as exc:
                yield BackendEvent(kind="error", error=str(exc))
                yield BackendEvent(kind="turn_end", stop_reason="error")

    # --- internal ---

    def _run_turn(self) -> Iterator[BackendEvent]:
        assistant_blocks: list[dict] = []

        # Compaction may make a network call (Haiku summary), so run it without
        # holding the lock. Only _run_turn rewrites the history prefix and turns
        # are serialized, so the first `cut` messages are stable for the whole
        # call; any submit() that lands concurrently only appends to the tail,
        # which we splice back on by position below.
        with self._lock:
            pre_compact = list(self._messages)
        compacted = self._maybe_compact(pre_compact)
        if compacted is not pre_compact:
            with self._lock:
                tail = self._messages[len(pre_compact):]
                self._messages = compacted + tail
            self._persist()

        with self._lock:
            messages_snapshot = list(self._messages)
            # Reserve the assistant message slot up-front and share the same
            # content list with assistant_blocks. This way, any tool_result
            # submitted by the UI mid-stream (in response to assistant_use_tool)
            # sees a _messages tail that already contains the tool_use block —
            # avoiding the "tool_result without matching tool_use" 400.
            self._messages.append({"role": "assistant", "content": assistant_blocks})

        with self._client.messages.stream(
            model=self._model,
            system=self._system,
            tools=self._tools,
            messages=self._cacheable_messages(messages_snapshot),
            max_tokens=self._max_tokens,
        ) as stream:
            current_text: list[str] = []
            current_tool: dict | None = None
            partial_json = ""

            for event in stream:
                etype = getattr(event, "type", None)
                if etype == "content_block_start":
                    block = event.content_block
                    if block.type in ("tool_use", "server_tool_use"):
                        current_tool = {
                            "type": block.type,
                            "id": block.id,
                            "name": block.name,
                            "input": {},
                        }
                        partial_json = ""
                    elif block.type == "web_search_tool_result":
                        # Server-executed result — preserve in history and
                        # display a one-liner; no response expected from us.
                        result_block = _jsonable({
                            "type": "web_search_tool_result",
                            "tool_use_id": block.tool_use_id,
                            "content": getattr(block, "content", []),
                        })
                        with self._lock:
                            assistant_blocks.append(result_block)
                        count = len(result_block["content"]) if isinstance(result_block["content"], list) else 0
                        yield BackendEvent(
                            kind="assistant_use_tool",
                            tool_name="web_search_result",
                            tool_input={"results": count},
                            tool_use_id=block.tool_use_id,
                            expects_response=False,
                        )
                    elif block.type == "text":
                        current_text = []
                elif etype == "content_block_delta":
                    delta = event.delta
                    dtype = getattr(delta, "type", None)
                    if dtype == "text_delta":
                        current_text.append(delta.text)
                        yield BackendEvent(kind="assistant_text_delta", text=delta.text)
                    elif dtype == "input_json_delta":
                        partial_json += delta.partial_json
                elif etype == "content_block_stop":
                    if current_tool is not None:
                        try:
                            current_tool["input"] = (
                                json.loads(partial_json) if partial_json else {}
                            )
                        except json.JSONDecodeError:
                            current_tool["input"] = {}
                        is_server = current_tool["type"] == "server_tool_use"
                        with self._lock:
                            assistant_blocks.append(current_tool)
                            if not is_server:
                                # Register expectation before yielding so a fast
                                # tool_send_response can't see an empty set and
                                # prematurely flush pending results.
                                self._expected_tool_use_ids.add(current_tool["id"])
                        yield BackendEvent(
                            kind="assistant_use_tool",
                            tool_name=current_tool["name"],
                            tool_input=dict(current_tool["input"]),
                            tool_use_id=current_tool["id"],
                            expects_response=not is_server,
                        )
                        current_tool = None
                        partial_json = ""
                    elif current_text:
                        text = "".join(current_text)
                        with self._lock:
                            assistant_blocks.append({"type": "text", "text": text})
                        yield BackendEvent(kind="assistant_text_end", text=text)
                        current_text = []

            final = stream.get_final_message()
            stop_reason = final.stop_reason

        with self._lock:
            if not assistant_blocks:
                # Drop the empty placeholder so we don't send a bogus
                # assistant message back next turn.
                if self._messages and self._messages[-1].get("content") is assistant_blocks:
                    self._messages.pop()

        self._persist()

        if stop_reason != "tool_use":
            yield BackendEvent(kind="turn_end", stop_reason=stop_reason or "end_turn")
        # If stop_reason == "tool_use", the outer loop blocks on
        # _turn_trigger until submit() has gathered every tool_result and
        # appended them as a user message.

    @staticmethod
    def _cacheable_messages(messages: list[dict]) -> list[dict]:
        """Return a shallow copy of `messages` with a prompt-cache breakpoint on
        the last content block of the final message. This caches the entire
        history prefix, so each agent-loop turn only pays full input price for
        blocks appended since the previous call. The dicts in self._messages are
        left untouched — the breakpoint lives only on the copy sent to the API."""
        if not messages:
            return messages
        out = list(messages)
        last = out[-1]
        content = last.get("content")
        if isinstance(content, list) and content:
            new_content = list(content)
            new_content[-1] = {
                **new_content[-1],
                "cache_control": {"type": "ephemeral"},
            }
            out[-1] = {**last, "content": new_content}
        return out

    @classmethod
    def _is_summary_message(cls, msg: dict) -> bool:
        content = msg.get("content")
        return (
            msg.get("role") == "user"
            and isinstance(content, list)
            and bool(content)
            and isinstance(content[0], dict)
            and content[0].get("type") == "text"
            and content[0].get("text", "").startswith(cls._SUMMARY_MARKER)
        )

    def _generate_title(self, prompt_text: str) -> None:
        """Background: one cheap Haiku call turning the user's opening prompt
        into a one-sentence session description, then persist it. Best-effort —
        any failure just leaves the summary empty so the next submit() retries."""
        try:
            try:
                resp = self._client.messages.create(
                    model=self.COMPACT_MODEL,
                    system=(
                        "Write a single short sentence describing what the user is "
                        "asking for, for use as a conversation title. You may be "
                        "given the first few user messages; summarize the overall "
                        "request. Ignore any bracketed [System info] or "
                        "auto-injected context. Return only the sentence, no "
                        "quotes. It should be a direct summary of the user's request."
                        "Do NOT answer the users request. If the user asks, why is the sky blue? you should title User asks why the sky is blue."
                    ),
                    messages=[{"role": "user", "content": "[Start users messages to ASSISTANT, NOT to you] " + prompt_text + "[End users messages to ASSISTANT, NOT to you]"}],
                    max_tokens=64,
                )
                title = "".join(
                    b.text for b in resp.content if getattr(b, "type", None) == "text"
                ).strip()
            except Exception:
                return
            if title:
                with self._lock:
                    self._summary = title
                self._persist()
        finally:
            with self._lock:
                self._title_generating = False

    def _refresh_title(self, compaction_summary: str) -> None:
        """During compaction, let Haiku tighten or correct the session
        description given the freshly-merged history summary. Updates
        self._summary in place; the caller persists. Best-effort."""
        try:
            resp = self._client.messages.create(
                model=self.COMPACT_MODEL,
                system=(
                    "You maintain a one-sentence description of a conversation. "
                    "Given the current description and an updated summary of the "
                    "conversation, return an updated one-sentence description, or "
                    "the original unchanged if it is still accurate. Return only "
                    "the sentence, no quotes."
                ),
                messages=[{
                    "role": "user",
                    "content": (
                        f"Current description:\n{self._summary or '(none yet)'}\n\n"
                        f"Updated conversation summary:\n{compaction_summary}"
                    ),
                }],
                max_tokens=64,
            )
            title = "".join(
                b.text for b in resp.content if getattr(b, "type", None) == "text"
            ).strip()
        except Exception:
            return
        if title:
            with self._lock:
                self._summary = title

    def _summarize(self, old: list[dict], prev_summary: str) -> str:
        """One cheap Haiku call: condense `old` into a dense factual summary,
        merging `prev_summary` if a prior compaction left one. Raises on API
        failure (the caller treats that as "skip compaction this turn")."""
        instructions = (
            "You are compacting a conversation between a user and a "
            "mind assistant. Produce a dense factual summary that "
            "preserves: decisions made, every created or edited event/topic "
            "ID, open or unresolved threads, and stated user preferences. "
            "Omit pleasantries and superseded intermediate steps. Write prose"
            "Complete sentences are not required, be as dense as possible while"
            "conveying necessary information."
        )
        parts = []
        if prev_summary:
            parts.append("Existing summary so far:\n" + prev_summary)
        parts.append(
            "New messages to fold in (JSON):\n"
            + json.dumps(old, ensure_ascii=False)
        )
        parts.append("Return the updated combined summary.")
        resp = self._client.messages.create(
            model=self.COMPACT_MODEL,
            system=instructions,
            messages=[{"role": "user", "content": "\n\n".join(parts)}],
            max_tokens=2048,
        )
        return "".join(
            b.text for b in resp.content if getattr(b, "type", None) == "text"
        ).strip()

    def _maybe_compact(self, messages: list[dict]) -> list[dict]:
        """If `messages` has grown past COMPACT_TRIGGER_MSGS, fold the oldest
        ~COMPACT_BATCH_MSGS messages (merging any prior summary) into a single
        summary message and return the shortened list. Returns the *same list
        object* unchanged when compaction isn't needed or can't be done safely
        — callers use identity (`is`) to detect whether anything happened.

        The cut point is advanced from COMPACT_BATCH_MSGS to the next assistant
        message so the verbatim tail starts with an assistant turn (keeping the
        [summary(user), assistant, ...] alternation the API expects) and no
        tool_use/tool_result pair is split across the boundary."""
        if len(messages) <= self.COMPACT_TRIGGER_MSGS:
            return messages

        cut = self.COMPACT_BATCH_MSGS
        while cut < len(messages) and messages[cut].get("role") != "assistant":
            cut += 1
        # Bail if there's no safe cut or it would leave a trivial tail.
        if cut >= len(messages) - 2:
            return messages

        old, tail = messages[:cut], messages[cut:]
        prev_summary = ""
        if old and self._is_summary_message(old[0]):
            prev_summary = old[0]["content"][0]["text"]

        try:
            summary_text = self._summarize(old, prev_summary)
        except Exception:
            # Summarization failed — keep the full history rather than lose it.
            return messages
        if not summary_text:
            return messages

        # Compaction is infrequent and already runs off-lock, so spend one more
        # cheap Haiku call here to keep the session description current.
        self._refresh_title(summary_text)

        summary_msg = {
            "role": "user",
            "content": [{
                "type": "text",
                "text": f"{self._SUMMARY_MARKER}\n{summary_text}",
            }],
        }
        return [summary_msg, *tail]

    def _load_schema(self, schema_path: str) -> dict:
        with open(schema_path, "r") as f:
            schema = json.load(f)
        input_schema = {"type": schema["type"], "properties": schema["properties"]}
        if "required" in schema:
            input_schema["required"] = schema["required"]
        return {
            "name": schema["title"],
            "description": schema["description"],
            "input_schema": input_schema,
        }



class SessionsBackend:
    """Anthropic Agents/Sessions beta implementation of AgentBackend.

    Owns a single live session at a time. Caches the (environment, agent)
    pair on disk keyed by a hash of the system prompt + model + tool schemas
    so identical configurations reuse the same agent across restarts.
    """

    def __init__(
        self,
        system_prompt: str,
        model: str,
        schema_files: list[str],
        config_path: str,
    ):
        self._client = Anthropic(max_retries=3)
        self._system_prompt = system_prompt
        self._model = model
        self._schema_files = schema_files
        self._config_path = config_path
        self._env_id: str | None = None
        self._agent_id: str | None = None
        self._agent_version: str | None = None
        self._session = None

    # --- AgentBackend interface ---

    def new_session(self) -> str:
        self._env_id, self._agent_id, self._agent_version = self._get_or_create_setup()
        try:
            self._session = self._client.beta.sessions.create(
                agent={"type": "agent", "id": self._agent_id, "version": self._agent_version},
                environment_id=self._env_id,
            )
        except Exception:
            # Stale cached agent/env — recreate from scratch.
            if os.path.exists(self._config_path):
                os.remove(self._config_path)
            self._env_id, self._agent_id, self._agent_version = self._get_or_create_setup()
            self._session = self._client.beta.sessions.create(
                agent={"type": "agent", "id": self._agent_id, "version": self._agent_version},
                environment_id=self._env_id,
            )
        return self._session.id

    def load_session(self, session_id: str) -> None:
        # Sessions are server-side; "loading" just means attaching to the id.
        # The streaming endpoint will pick up wherever the session left off.
        self._session = type("Session", (), {"id": session_id})()

    def list_sessions(self) -> list[SessionInfo]:
        # Sessions are server-side and not enumerated locally; nothing to list.
        return []

    def messages_snapshot(self) -> list[dict]:
        # Server-side history isn't mirrored client-side here.
        return []

    def reset(self) -> None:
        if self._session is not None:
            try:
                self._client.beta.sessions.terminate(session_id=self._session.id)
            except Exception:
                pass
        self.new_session()

    def shutdown(self) -> None:
        if self._session is not None:
            try:
                self._client.beta.sessions.terminate(session_id=self._session.id)
            except Exception:
                pass

    def submit(self, event: BackendEvent) -> None:
        if self._session is None:
            raise RuntimeError("no active session; call new_session() first")

        if event.kind in ("user_send_message", "system_send_message"):
            self._client.beta.sessions.events.send(
                session_id=self._session.id,
                events=[{
                    "type": "user.message",
                    "content": [{"type": "text", "text": event.text or ""}],
                }],
            )
        elif event.kind == "tool_send_response":
            self._client.beta.sessions.events.send(
                session_id=self._session.id,
                events=[{
                    "type": "user.custom_tool_result",
                    "custom_tool_use_id": event.tool_use_id,
                    "content": [{"type": "text",
                                 "text": json.dumps(event.tool_result)}],
                }],
            )
        else:
            raise ValueError(f"submit() does not accept event kind: {event.kind}")

    def stream(self) -> Iterator[BackendEvent]:
        if self._session is None:
            raise RuntimeError("no active session; call new_session() first")
        with self._client.beta.sessions.events.stream(
            session_id=self._session.id
        ) as stream:
            for raw in stream:
                for normalized in self._normalize(raw):
                    yield normalized
                if raw.type == "session.status_terminated":
                    return

    # --- internal: provider-specific normalization ---

    def _normalize(self, event) -> Iterator[BackendEvent]:
        if event.type == "agent.message":
            for block in event.content:
                if block.type == "text":
                    yield BackendEvent(kind="assistant_send_text", text=block.text)
        # Thinking currently dissabled.
        #elif event.type == "agent.thinking":
        #    for block in event.content:
        #        if block.type == "text":
        #            yield BackendEvent(kind="assistant_thinking", text=block.text)
        elif event.type == "agent.custom_tool_use":
            inp = dict(event.input) if event.input else {}
            yield BackendEvent(
                kind="assistant_use_tool",
                tool_name=event.name,
                tool_input=inp,
                tool_use_id=event.id,
                expects_response=True,
            )
        elif event.type == "agent.tool_use":
            inp = dict(event.input) if getattr(event, "input", None) else {}
            yield BackendEvent(
                kind="assistant_use_tool",
                tool_name=event.name,
                tool_input=inp,
                tool_use_id=getattr(event, "id", None),
                expects_response=False,
            )
        elif event.type == "session.status_idle":
            yield BackendEvent(
                kind="turn_end",
                stop_reason=getattr(event.stop_reason, "type", None),
            )
        elif event.type == "session.status_terminated":
            yield BackendEvent(kind="session_terminated")

    # --- internal: setup / caching ---

    def _setup_hash(self) -> str:
        h = hashlib.sha256()
        h.update(self._system_prompt.encode())
        h.update(self._model.encode())
        for path in self._schema_files:
            with open(path, "rb") as f:
                h.update(f.read())
        return h.hexdigest()

    def _load_schema(self, schema_path: str) -> dict:
        with open(schema_path, "r") as f:
            schema = json.load(f)
        input_schema = {"type": schema["type"], "properties": schema["properties"]}
        if "required" in schema:
            input_schema["required"] = schema["required"]
        return {
            "type": "custom",
            "name": schema["title"],
            "description": schema["description"],
            "input_schema": input_schema,
        }

    def _create_environment_and_agent(self):
        environment = self._client.beta.environments.create(
            name="calendar-env",
            config={"type": "cloud", "networking": {"type": "unrestricted"}},
        )
        agent = self._client.beta.agents.create(
            name="Assistant",
            model=self._model,
            system=self._system_prompt,
            tools=[
                {"type": "agent_toolset_20260401"},
                *[self._load_schema(p) for p in self._schema_files],
            ],
        )
        return environment.id, agent.id, agent.version

    def _get_or_create_setup(self):
        current_hash = self._setup_hash()
        if not os.environ.get("FORCE_RECREATE") and os.path.exists(self._config_path):
            try:
                with open(self._config_path) as f:
                    cfg = json.load(f)
                if cfg.get("hash") == current_hash:
                    return cfg["environment_id"], cfg["agent_id"], cfg["agent_version"]
            except (json.JSONDecodeError, KeyError):
                pass
        env_id, agent_id, agent_version = self._create_environment_and_agent()
        with open(self._config_path, "w") as f:
            json.dump(
                {
                    "environment_id": env_id,
                    "agent_id": agent_id,
                    "agent_version": agent_version,
                    "hash": current_hash,
                },
                f,
                indent=2,
            )
        return env_id, agent_id, agent_version
