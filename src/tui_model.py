import json
import os
import hashlib
import threading
import requests
from anthropic import Anthropic
import datetime
import calendar
from datetime import date
import re
from dataclasses import dataclass
from typing import Iterator, Literal, Protocol, runtime_checkable

from rich.markup import render as render_markup, escape as escape_markup
from rich.errors import MarkupError

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, VerticalScroll
from textual.widgets import (
    Footer,
    Header,
    Input,
    Static,
    RichLog,
    Button,
    ContentSwitcher,
    Tabs,
    Tab,
    DataTable,
)

MonthSelected = "04"

API_URL = "http://localhost:8080/api"
CONFIG_PATH = os.environ['HOME'] + "/.config/aime-assistant/agents_config.json"
PREFS_PATH = os.environ['HOME'] + "/.config/aime-assistant/tui_prefs.json"
SYSTEM_PROMPT_PATH = "../resources/prompts/system_prompt.md"
DEFAULT_THEME = "gruvbox"


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


@runtime_checkable
class AgentBackend(Protocol):
    """Provider-agnostic interface for an agentic conversation backend."""

    def new_session(self) -> str:
        """Start a fresh session. Returns the session id."""
        ...

    def load_session(self, session_id: str) -> None:
        """Resume a previously-started session by id."""
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

        self._session_id: str | None = None
        self._messages: list[dict] = []
        self._pending_tool_results: list[dict] = []
        self._expected_tool_use_ids: set[str] = set()
        self._turn_trigger = threading.Event()
        self._terminated = threading.Event()
        self._lock = threading.Lock()

    # --- AgentBackend interface ---

    def new_session(self) -> str:
        with self._lock:
            self._messages = []
            self._pending_tool_results = []
            self._expected_tool_use_ids = set()
        self._terminated.clear()
        self._turn_trigger.clear()
        self._session_id = "msgs-" + hashlib.sha1(os.urandom(8)).hexdigest()[:12]
        return self._session_id

    def load_session(self, session_id: str) -> None:
        # No server-side state to resume — caller is expected to have any
        # prior history already, or to start a fresh new_session().
        self._session_id = session_id

    def reset(self) -> None:
        # Wake the stream loop so it observes termination and exits cleanly,
        # then start a fresh session.
        self._terminated.set()
        self._turn_trigger.set()
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
                self._turn_trigger.set()
        else:
            raise ValueError(f"submit() does not accept event kind: {event.kind}")

    def stream(self) -> Iterator[BackendEvent]:
        while True:
            self._turn_trigger.wait()
            self._turn_trigger.clear()
            if self._terminated.is_set():
                yield BackendEvent(kind="session_terminated")
                return
            try:
                yield from self._run_turn()
            except Exception as exc:
                yield BackendEvent(kind="error", error=str(exc))
                yield BackendEvent(kind="turn_end", stop_reason="error")

    # --- internal ---

    def _run_turn(self) -> Iterator[BackendEvent]:
        assistant_blocks: list[dict] = []

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
            system=self._system_prompt,
            tools=self._tools,
            messages=messages_snapshot,
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
                        result_block = {
                            "type": "web_search_tool_result",
                            "tool_use_id": block.tool_use_id,
                            "content": getattr(block, "content", []),
                        }
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

        if stop_reason != "tool_use":
            yield BackendEvent(kind="turn_end", stop_reason=stop_reason or "end_turn")
        # If stop_reason == "tool_use", the outer loop blocks on
        # _turn_trigger until submit() has gathered every tool_result and
        # appended them as a user message.

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


#UI, API Provider agnostic from here on.

def load_prefs() -> dict:
    try:
        with open(PREFS_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_prefs(prefs: dict) -> None:
    try:
        with open(PREFS_PATH, "w") as f:
            json.dump(prefs, f, indent=2)
    except OSError:
        pass

with open(SYSTEM_PROMPT_PATH) as f:
    SYSTEM_PROMPT = f.read()

TOOL_NAME_MAP = {
    "FilterUsersEvents": "get_events",
    "EditEvent": "replace_event",
    "CreateEvent": "create_event",
    "FilterTopics": "get_topics",
    "CreateTopic": "create_topic",
    "ReplaceTopic": "replace_topic",
    "GetTopicContents": "get_topic_contents",
    "ReplaceTopicContents": "replace_topic_contents",
    "EditTopicContents": "edit_topic_contents",
}

MONTH_STR_TO_NUMBER_MAP = {
    "one": "01",
    "two": "02",
    "three": "03",
    "four": "04",
    "five": "05",
    "six": "06",
    "seven": "07",
    "eight": "08",
    "nine": "09",
    "ten": "10",
    "eleven": "11",
    "twelve": "12",
}

MONTH_NUMBER_TO_STR_MAP = [
    "", "one", "two", "three", "four", "five", "six",
    "seven", "eight", "nine", "ten", "eleven", "twelve",
]

SCHEMA_FILES = [
    "../resources/tools/api_request_schema.json",
    "../resources/tools/api_replace_event_schema.json",
    "../resources/tools/api_create_event_schema.json",
    "../resources/tools/api_request_topics_schema.json",
    "../resources/tools/api_create_topic_schema.json",
    "../resources/tools/api_replace_topic_schema.json",
    "../resources/tools/api_get_topic_contents_schema.json",
    "../resources/tools/api_replace_topic_contents_schema.json",
    "../resources/tools/api_edit_topic_contents_schema.json",
]

AGENT_MODEL = "claude-sonnet-4-6"


def _truncate_for_log(value, limit: int = 60) -> str:
    if value is None:
        return ""
    s = str(value).replace("\n", " ").strip()
    if len(s) <= limit:
        return s
    return s[: limit - 1] + "…"


def _format_tool_details(name: str, inp: dict) -> str:
    """Return a one-line description of what a tool call is doing, based on
    its input. Empty string if nothing notable."""
    if not isinstance(inp, dict):
        return ""
    parts: list[str] = []

    if name == "FilterUsersEvents":
        if inp.get("filter_by_date"):
            parts.append(
                f"date {inp.get('start_date', '?')} → {inp.get('end_date', '?')}"
            )
        if inp.get("filter_by_category"):
            parts.append(f"category={inp.get('category')}")
        if inp.get("filter_by_title"):
            parts.append(f"title~'{_truncate_for_log(inp.get('title'), 30)}'")
        if inp.get("sort_order"):
            parts.append(f"sort={inp.get('sort_order')}")
    elif name == "CreateEvent":
        title = _truncate_for_log(inp.get("title"), 40) or "?"
        date_ = inp.get("date") or "?"
        parts.append(f"\"{title}\" on {date_}")
        if inp.get("time"):
            parts.append(f"at {inp['time']}")
        if inp.get("category"):
            parts.append(f"#{inp['category']}")
    elif name == "EditEvent":
        parts.append(f"id={inp.get('id', '?')}")
        for field in ("title", "date", "time", "category", "summary"):
            if inp.get(field):
                parts.append(f"{field}={_truncate_for_log(inp[field], 30)}")
    elif name == "FilterTopics":
        if inp.get("filter_by_category"):
            parts.append(f"category={inp.get('category')}")
        if inp.get("filter_by_title"):
            parts.append(f"title~'{_truncate_for_log(inp.get('title'), 30)}'")
        if not parts:
            parts.append("listing all")
    elif name == "CreateTopic":
        parts.append(f"\"{_truncate_for_log(inp.get('title'), 40) or '?'}\"")
        if inp.get("category"):
            parts.append(f"#{inp['category']}")
    elif name == "ReplaceTopic":
        parts.append(f"id={inp.get('id', '?')}")
        if inp.get("title"):
            parts.append(f"title={_truncate_for_log(inp['title'], 30)}")
    elif name == "GetTopicContents":
        parts.append(f"id={inp.get('id', '?')}")
    elif name == "ReplaceTopicContents":
        parts.append(f"id={inp.get('id', '?')}")
        if inp.get("contents") is not None:
            parts.append(f"len={len(str(inp['contents']))}")
    elif name == "EditTopicContents":
        parts.append(f"id={inp.get('id', '?')}")
        if "old_string" in inp:
            parts.append(f"old='{_truncate_for_log(inp.get('old_string'), 30)}'")
        if "new_string" in inp:
            parts.append(f"new='{_truncate_for_log(inp.get('new_string'), 30)}'")
    else:
        # Built-in / unknown tools — best-effort common keys.
        for key in ("query", "url", "command", "path", "pattern"):
            if inp.get(key):
                parts.append(f"{key}=\"{_truncate_for_log(inp[key], 60)}\"")
        if not parts:
            keys = [k for k in inp.keys() if k != "tool_name"][:3]
            if keys:
                parts.append("with " + ", ".join(keys))

    return ", ".join(parts)


def _format_tool_response(name: str, result) -> str:
    """Return a one-line description of what a tool call returned, based on
    its result. Mirrors _format_tool_details. Empty string if nothing notable."""
    if isinstance(result, dict) and "error" in result:
        return f"error: {_truncate_for_log(result.get('error'), 80)}"

    parts: list[str] = []

    if name in ("FilterUsersEvents", "FilterTopics"):
        if isinstance(result, list):
            items = result
        elif isinstance(result, dict):
            items = result.get("events") or result.get("topics") or []
        else:
            items = []
        label = "event" if name == "FilterUsersEvents" else "topic"
        parts.append(f"{len(items)} {label}{'s' if len(items) != 1 else ''}")
        for it in items[:3]:
            if not isinstance(it, dict):
                continue
            title = _truncate_for_log(it.get("title") or it.get("name"), 30) or "?"
            if name == "FilterUsersEvents":
                parts.append(f"\"{title}\"@{it.get('date', '?')}")
            else:
                tid = it.get("id")
                parts.append(f"\"{title}\"" + (f"#{tid}" if tid is not None else ""))
        if len(items) > 3:
            parts.append(f"+{len(items) - 3} more")
    elif name in ("CreateEvent", "EditEvent", "CreateTopic", "ReplaceTopic",
                  "ReplaceTopicContents", "EditTopicContents"):
        if isinstance(result, dict):
            for key in ("id", "status", "ok", "success"):
                if key in result:
                    parts.append(f"{key}={_truncate_for_log(result[key], 30)}")
            if not parts:
                keys = list(result.keys())[:3]
                if keys:
                    parts.append("keys: " + ", ".join(keys))
    elif name == "GetTopicContents":
        if isinstance(result, dict):
            contents = result.get("contents", "") or ""
            parts.append(f"len={len(str(contents))}")
            preview = _truncate_for_log(contents, 50)
            if preview:
                parts.append(f"preview='{preview}'")
    else:
        # Built-in / unknown tools — best-effort summary.
        if isinstance(result, list):
            parts.append(f"{len(result)} item{'s' if len(result) != 1 else ''}")
        elif isinstance(result, dict):
            for k, v in list(result.items())[:3]:
                parts.append(f"{k}={_truncate_for_log(v, 40)}")
        else:
            preview = _truncate_for_log(result, 60)
            if preview:
                parts.append(preview)

    return ", ".join(parts)


user_first_interaction = True

SPECIAL_TOPICS = [
    {
        "title": "About Me",
        "category": "personal",
        "summary": "Identity-level facts about the user (name, location, relationships, personality).",
    },
    {
        "title": "Pending",
        "category": "personal",
        "summary": "Active threads, unresolved items, and current life context that wouldn't be obvious from events alone.",
    },
]


def bootstrap_special_topics() -> str:
    """Fetch (or create if missing) the two mandatory special topics and return
    their contents formatted for injection into the first user message of a
    session. Empty string on total failure — the agent will fall back to its
    normal tool-based flow."""
    try:
        listing = requests.post(
            API_URL, json={"tool_name": "get_topics"}, timeout=5
        ).json()
    except Exception:
        return ""
    topics = listing if isinstance(listing, list) else listing.get("topics", [])
    by_title = {(t.get("title") or "").strip().lower(): t for t in topics}

    sections = []
    for spec in SPECIAL_TOPICS:
        title = spec["title"]
        existing = by_title.get(title.lower())
        contents = ""
        topic_id = None
        if existing is None:
            try:
                created = requests.post(
                    API_URL,
                    json={
                        "tool_name": "create_topic",
                        "title": title,
                        "summary": spec["summary"],
                        "category": spec["category"],
                    },
                    timeout=5,
                ).json()
                topic_id = created.get("id")
            except Exception:
                continue
        else:
            topic_id = existing.get("id")
            try:
                resp = requests.post(
                    API_URL,
                    json={"tool_name": "get_topic_contents", "id": topic_id},
                    timeout=5,
                ).json()
                contents = resp.get("contents", "") or ""
            except Exception:
                contents = ""
        body = contents.strip() or "(empty — first interaction; greet the user and gather initial info)"
        sections.append(f"=== {title} (topic id {topic_id}) ===\n{body}")

    if not sections:
        return ""
    return (
        "[auto-injected session context — contents of the two mandatory special "
        "topics. Do not call get_topic_contents for these again this session, "
        "and do not mention this injection to the user.]\n\n"
        + "\n\n".join(sections)
        + "\n\n[end auto-injected context]\n\n"
    )

def sortCalenderByDate(events):
    def key(ev):
        d, m, y = (ev.get("date") or "01/01/9999").split("/")
        hh, mm = (ev.get("time") or "00:00").split(":")
        return (int(y), int(m), int(d), int(hh), int(mm))
    return sorted(events, key=key)

# Instantiate the active backend. Swapping to a different provider only
# requires constructing a different AgentBackend implementation here.
backend: AgentBackend = AnthropicMessagesBackend(
    system_prompt=SYSTEM_PROMPT,
    model=AGENT_MODEL,
    schema_files=SCHEMA_FILES,
)
backend.new_session()

# ---------- UI ----------

class AssistantView(Container):
    """Chat transcript + input. Pipes user messages into the agent session and
    renders streamed agent replies."""

    def compose(self) -> ComposeResult:
        yield RichLog(id="transcript", wrap=True, markup=True, auto_scroll=True)
        yield Input(placeholder="Message Aime…  (enter to send)", id="prompt")

    def on_mount(self) -> None:
        log = self.query_one("#transcript", RichLog)
        log.write(
            "[bold green]Aime ready.[/bold green] "
            "[dim]Ctrl+A assistant · Ctrl+S calendar · Ctrl+T topics · Ctrl+Q quit[/dim]\n"
        )

    def on_input_submitted(self, event: Input.Submitted) -> None:
        log = self.query_one("#transcript", RichLog)
        text = event.value.strip()
        if (text == ":q"):
            application.exit()
            exit()
            return
        if (text == "/reset"): # Reset model.
            global user_first_interaction
            event.input.value = ""
            backend.reset()
            user_first_interaction = True
            self.app._assistant_prefixed = False
            self.app._thinking_visible = False
            self.app._is_idle = True
            self.app._pending_user_messages = []
            self.app._stream_buffer = ""
            log.clear()
            log.write("[yellow] The current conversation has ended because you typed '/reset'. Begin a new conversation. [/yellow]")
            self.app.run_worker(
                self.app._stream_events,
                thread=True,
                exclusive=True,
                name="agent-stream",
            )
            return
        if (text == "/toggle_log_model_thinking"):
            self.app._log_model_thinking = not self.app._log_model_thinking
            log.write(f"[dim]Log model thinking set to: {self.app._log_model_thinking}[/dim]")
            event.input.value = ""
            return
        if not text:
            return
        event.input.value = ""
        self.app.send_user_message(text)

    def focus_input(self) -> None:
        self.query_one("#prompt", Input).focus()


class VimDataTable(DataTable):
    """DataTable with vim-style hjkl navigation in addition to arrow keys.

    After any cursor movement (arrow keys or hjkl), dispatches to a direction-
    specific callback on the enclosing CalendarView, passing the table and the
    Tabs widget so the callback can read/update either.
    """

    BINDINGS = [
        Binding("h", "cursor_left", "Left", show=False),
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("l", "cursor_right", "Right", show=False),
    ]

    def _dispatch_cursor(self, direction: str) -> None:
        view = self.screen.query_one(CalendarView)
        tabs = view.query_one(Tabs)
        handler = getattr(view, f"on_cursor_{direction}", None)
        if handler is not None:
            handler(self, tabs)

    def action_cursor_up(self) -> None:
        if self.cursor_row == 0:
            tabs = self.screen.query_one(CalendarView).query_one(Tabs)
            tabs.focus()
            return
        super().action_cursor_up()
        self._dispatch_cursor("up")

    def action_cursor_down(self) -> None:
        super().action_cursor_down()
        self._dispatch_cursor("down")

    def action_cursor_left(self) -> None:
        super().action_cursor_left()
        self._dispatch_cursor("left")

    def action_cursor_right(self) -> None:
        super().action_cursor_right()
        self._dispatch_cursor("right")

class VimTabs(Tabs):
    """Tabs with vim-style h/l and a j/down binding that drops focus into the
    calendar's DataTable, so arrow/vim keys flow seamlessly between the two."""

    BINDINGS = [
        Binding("h", "previous_tab", "Previous tab", show=False),
        Binding("l", "next_tab", "Next tab", show=False),
        Binding("j", "focus_table", "Focus table", show=False),
        Binding("down", "focus_table", "Focus table", show=False),
    ]

    def action_focus_table(self) -> None:
        table = self.screen.query_one(CalendarView).query_one(VimDataTable)
        table.focus()

class CalendarView(Container):
    """Direct view of the events store. Hits the same /api endpoint the agent
    uses, with tool_name=get_events."""

    selected_day: int | None = None
    current_date = datetime.datetime.now()
    month_name = calendar.month_name[current_date.month]
    day_int = current_date.day

    def compose(self) -> ComposeResult:
        with Horizontal(id="calendar-toolbar"):
            yield Static("[bold]Your events[/bold]", id="calendar-title")
            yield Button("Refresh", id="calendar-refresh", variant="primary")
        yield VimTabs(
            Tab("Jan", id="one"),
            Tab("Feb", id="two"),
            Tab("Mar", id="three"),
            Tab("Apr", id="four"),
            Tab("May", id="five"),
            Tab("Jun", id="six"),
            Tab("Jul", id="seven"),
            Tab("Aug", id="eight"),
            Tab("Sep", id="nine"),
            Tab("Oct", id="ten"),
            Tab("Nov", id="eleven"),
            Tab("Dec", id="twelve"),
        )
        with Horizontal(id="calendar-body"):
            yield VimDataTable()
            yield VerticalScroll(Static("", id="calendar-list"), id="calendar-scroll")

    def on_tabs_tab_activated(self, event: Tabs.TabActivated) -> None:
        global MonthSelected
        """Handle TabActivated message sent by Tabs."""
        tabs = self.query_one(Tabs)
        active_tab = tabs.active_tab
        if active_tab is not None:
            MonthSelected = MONTH_STR_TO_NUMBER_MAP[active_tab.id]
            self.refresh_events()
            self.refresh_table()

    def on_cursor_up(self, table: "VimDataTable", tabs: Tabs) -> None:
        """Called after the calendar cursor moves up. Fill in custom behavior."""
        pass

    def on_cursor_down(self, table: "VimDataTable", tabs: Tabs) -> None:
        """Called after the calendar cursor moves down. Fill in custom behavior."""
        pass

    def on_cursor_left(self, table: "VimDataTable", tabs: Tabs) -> None:
        """Called after the calendar cursor moves left. Fill in custom behavior."""
        pass

    def on_cursor_right(self, table: "VimDataTable", tabs: Tabs) -> None:
        """Called after the calendar cursor moves right. Fill in custom behavior."""
        pass

    def on_data_table_cell_selected(self, event: DataTable.CellSelected) -> None:
        try:
            first_line = render_markup(str(event.value)).plain.split("\n", 1)[0]
            first_line = first_line.strip()
            day_number_selected = int(re.search(r'\d+', first_line).group())
        except (ValueError, MarkupError, AttributeError):
            self.selected_day = 0
            return
        if day_number_selected < 1:
            return
        self.selected_day = day_number_selected
        self.refresh_events()

    def refresh_table(self):
        table = self.query_one(DataTable)
        table.clear(columns=True)

        total_w = table.size.width
        col_w = max(3, total_w // 9) + 1

        for day in ("Sunday", "Monday", "Tuesday", "Wednesday",
                    "Thursday", "Friday", "Saturday"):
            table.add_column(day, width=col_w)

        titles_by_day: dict[int, list[str]] = {}
        try:
            response = requests.post(
                API_URL,
                json={"tool_name": "get_events",
                      "sort_order": "asc",
                      "filter_by_date": True,
                      "start_date": "00/" + MonthSelected + "/2026",
                      "end_date": "40/" + MonthSelected + "/2026",
                      },
                timeout=5,
            )
            if response.ok:
                data = response.json()
                events = data if isinstance(data, list) else data.get("events", [])
                for ev in events:
                    event_date = ev.get("date", "")
                    title = ev.get("title") or ev.get("name") or "(untitled)"

                    try:
                        day_num = int(event_date.split("/")[0])
                    except (ValueError, IndexError):
                        continue
                    titles_by_day.setdefault(day_num, []).append(title)

        except Exception:
            pass

        first_date = date(2026, int(MonthSelected), 1)
        first_day_of_month = first_date.isoweekday()

        total_h = table.size.height or 16
        row_h = max(1, (total_h - 1) // 6) + 1
        day_numbers = [1, 2, 3, 4, 5, 6, 7]
        for d in range(7):
            day_numbers[d] = max(0, day_numbers[d] - first_day_of_month)

        for _ in range(6):
            cells = []
            for n in day_numbers: # n is the day number, it corresponds to the day_numbers list. Could be done without list but oh well.
                try:
                    cell = ""
                    # will return error if date is not in the month. This causes the date to empty which is desired.
                    validation_date = date(2026, int(MonthSelected), n)
                    if (self.day_int == n and int(MonthSelected) == self.current_date.month):
                        cell = "[bold white blink] >" + str(n) + "< today...[/bold white blink]"
                    else:
                        cell = "[grey]" + str(n) + "[/grey]"
                except:
                    cell = "_"
                for title in titles_by_day.get(n, []):
                    if len(title) > 20:
                        cell += f"\n[dim]•{title[:20]}...[/dim]"
                    else:
                        cell += f"\n[dim]•{title}[/dim]"
                cells.append(cell)
            table.add_row(*cells, height=row_h)
            new_week_first_day = day_numbers[6] + 1
            for i in range(7):
                day_numbers[i] = new_week_first_day + i

        table.zebra_stripes = True

    def on_mount(self) -> None:
        self.refresh_events()
        self.refresh_table()
        tabs = self.query_one(Tabs)
        tabs.active = MONTH_NUMBER_TO_STR_MAP[self.current_date.month]

    def focus_table(self) -> None:
        self.query_one(VimDataTable).focus()

    def on_resize(self, event) -> None:
        self.refresh_table()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "calendar-refresh":
            self.refresh_events()
            self.refresh_table()

    def refresh_events(self) -> None:
        target = self.query_one("#calendar-list", Static)
        target.update("[dim]loading…[/dim]")
        if (self.selected_day is None) or (self.selected_day == 0):
            target.update("[dim]select a day in the calendar[/dim]")
            if (self.selected_day == 0):
                target.update("[dim] Day does not belong to this month[/dim]")
            return
        # Should only run if it's a valid day in the month.
        day_str = f"{self.selected_day:02d}"
        try:
            response = requests.post(
                API_URL,
                json={"tool_name": "get_events",
                      "sort_order": "asc",
                      "filter_by_date": True,
                      "start_date": f"{day_str}/{MonthSelected}/2026",
                      "end_date": f"{day_str}/{MonthSelected}/2026",
                      },
                timeout=5,
            )
            data = response.json() if response.ok else {"error": response.text}
        except Exception as exc:
            target.update(f"[red]error:[/red] {exc}")
            return

        events = data if isinstance(data, list) else data.get("events", [])
        if not events:
            target.update(f"[dim]no events on {day_str}/{MonthSelected}[/dim]")
            return

        sortCalenderByDate(events)
        lines = []
        for ev in events:
            date = ev.get("date", "")
            time_ = ev.get("time", "")
            title = ev.get("title") or ev.get("name") or "(untitled)"
            category = ev.get("category", "")
            summary = ev.get("summary", "")
            header = f"[bold cyan]{date}[/bold cyan]"
            if time_:
                header += f" [cyan]{time_}[/cyan]"
            if category:
                header += f"  [dim]#{category}[/dim]"
            lines.append(f"{header}\n  [bold]{title}[/bold]")
            if summary:
                lines.append(f"  [dim]{summary}[/dim]")
            lines.append("")
        target.update("\n".join(lines).rstrip())

class TopicView(Container):
    """Direct view of the topics store. tool_name=get_topics."""

    def compose(self) -> ComposeResult:
        with Horizontal(id="topic-toolbar"):
            yield Static("[bold]Your topics[/bold]", id="topic-title")
            yield Button("Refresh", id="topic-refresh", variant="primary")
        yield VerticalScroll(Static("", id="topic-list"), id="topic-scroll")

    def on_mount(self) -> None:
        self.refresh_topics()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "topic-refresh":
            self.refresh_topics()

    def refresh_topics(self) -> None:
        target = self.query_one("#topic-list", Static)
        target.update("[dim]loading…[/dim]")
        try:
            response = requests.post(
                API_URL, json={"tool_name": "get_topics"}, timeout=5
            )
            data = response.json() if response.ok else {"error": response.text}
        except Exception as exc:
            target.update(f"[red]error:[/red] {exc}")
            return

        topics = data if isinstance(data, list) else data.get("topics", [])
        if not topics:
            target.update("[dim]no topics yet[/dim]")
            return

        lines = []
        for tp in topics:
            title = tp.get("title") or tp.get("name") or "(untitled)"
            category = tp.get("category", "")
            summary = tp.get("summary", "")
            head = f"[bold cyan]{title}[/bold cyan]"
            if category:
                head += f"  [dim]#{category}[/dim]"
            lines.append(head)
            if summary:
                lines.append(f"  [dim]{summary}[/dim]")
            lines.append("")
        target.update("\n".join(lines).rstrip())


class Aime(App):
    CSS_PATH = "../resources/style/user_prompt.css"
    TITLE = "Aime"
    SUB_TITLE = "an extension of your mind"

    BINDINGS = [
        Binding("ctrl+a", "mode('assistant')", "Assistant"),
        Binding("ctrl+s", "mode('calendar')", "Calendar"),
        Binding("ctrl+t", "mode('topics')", "Topics"),
        Binding("ctrl+q", "quit", "Quit"),
    ]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with ContentSwitcher(initial="assistant", id="modes"):
            yield AssistantView(id="assistant")
            yield CalendarView(id="calendar")
            yield TopicView(id="topics")
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = "assistant"
        # Open the agent event stream in a background thread. Every event the
        # server sends (agent text, tool calls, status changes) gets handed to
        # _handle_event on the UI thread via call_from_thread.
        self.run_worker(
            self._stream_events,
            thread=True,
            exclusive=True,
            name="agent-stream",
        )
        self.query_one(AssistantView).focus_input()
        self.theme = load_prefs().get("theme", DEFAULT_THEME)

    def watch_theme(self, old_theme: str, new_theme: str) -> None:
        if new_theme is None:
            return
        prefs = load_prefs()
        if prefs.get("theme") == new_theme:
            return
        prefs["theme"] = new_theme
        save_prefs(prefs)

    # --- mode switching ---

    def action_mode(self, mode: str) -> None:
        self.query_one("#modes", ContentSwitcher).current = mode
        self.sub_title = mode
        if mode == "assistant":
            self.query_one(AssistantView).focus_input()
        elif mode == "calendar":
            view = self.query_one(CalendarView)
            view.refresh_events()
            view.focus_table()
        elif mode == "topics":
            self.query_one(TopicView).refresh_topics()

    # --- agent bridge ---

    def send_user_message(self, text: str) -> None:
        self.app._message_count += 1
        log = self.query_one("#transcript", RichLog)
        if not self._is_idle:
            self._pending_user_messages.append(text)
            log.write(
                f"\n[bold cyan]you[/bold cyan] [dim](queued — will send when Aime is done)[/dim]  {text}"
            )
            return
        self._dispatch_user_message(text)

    def _dispatch_user_message(self, text: str) -> None:
        global user_first_interaction
        day_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        log = self.query_one("#transcript", RichLog)
        log.write(f"\n[bold cyan]you[/bold cyan]  {text}")
        log.write("[dim]thinking…[/dim]")
        self._thinking_visible = True
        date_time = datetime.datetime.now()
        day_of_week = date_time.weekday()
        date_time_string = date_time.strftime("%d/%m/%Y, %H:%M") # Convert to string
        date_message = "[System info] Accurate date: " + day_names[day_of_week] + ", " + date_time_string + "do not tell this to the user unless relevant. Base decisions based off of THIS date, not any previous ones. [End System Info]"
        text = date_message + " " + text
        if user_first_interaction:
            bootstrap = bootstrap_special_topics()
            if bootstrap:
                text = bootstrap + text
            user_first_interaction = False
        if self.app._message_count > 15:
            text = "[System suggestion] The current conversation is growing long. Strongly urge the user that these steps are followed: Ask the user if they want you to summarize the context and put it in pending and mark it as previous conversation, You strongly suggest the user types '/reset' to reset the context. Still follow what the user asked if they do not choose to do this. Make this message to them brief. Explain briefly the effects of an infinitely growing model context. [End System Suggestion]" + text
        try:
            backend.submit(BackendEvent(kind="user_send_message", text=text))
            self._is_idle = False
        except Exception as exc:
            log.write(f"[red]send failed:[/red] {exc}")

    def _stream_events(self) -> None:
        """Runs on a worker thread. Forwards normalized backend events to the UI."""
        try:
            for event in backend.stream():
                self.call_from_thread(self._handle_event, event)
                if event.kind == "session_terminated":
                    return
        except Exception as exc:
            self.call_from_thread(
                self._log_line, f"[red]stream error:[/red] {exc}"
            )

    _thinking_visible = False
    _log_model_thinking = False
    _assistant_prefixed = False
    _message_count = 0
    _is_idle = True
    _pending_user_messages: list[str] = []
    _stream_buffer: str = ""

    def _clear_thinking(self) -> None:
        if self._thinking_visible:
            # RichLog can't retract a line, so we just drop a small separator
            # the next time real text arrives. (The "thinking…" stays as part
            # of the scrollback — acceptable for a transcript.)
            self._thinking_visible = False

    def _log_line(self, text: str) -> None:
        self.query_one("#transcript", RichLog).write(text)

    def _safe_write(self, log: RichLog, text: str) -> None:
        try:
            log.write(render_markup(text))
        except Exception:
            log.write(escape_markup(text))
            log.write("[bold red] Pretty output is disabled. Model made small mistake in formatting response.[/bold red]")

    def _ensure_assistant_prefix(self, log: RichLog) -> None:
        if not self._assistant_prefixed:
            log.write("[bold red]aime[/bold red]")
            self._assistant_prefixed = True

    def _stream_flush_lines(self, log: RichLog) -> None:
        """Flush any complete lines in _stream_buffer to the log. Partial trailing
        text stays buffered until the next delta or assistant_text_end."""
        if "\n" not in self._stream_buffer:
            return
        head, _, tail = self._stream_buffer.rpartition("\n")
        for line in head.split("\n"):
            self._safe_write(log, line)
        self._stream_buffer = tail

    def _stream_flush_all(self, log: RichLog) -> None:
        if self._stream_buffer:
            for line in self._stream_buffer.split("\n"):
                if line:
                    self._safe_write(log, line)
            self._stream_buffer = ""

    def _handle_event(self, event: BackendEvent) -> None:
        log = self.query_one("#transcript", RichLog)

        if event.kind == "assistant_send_text":
            self._clear_thinking()
            self._stream_flush_all(log)
            self._ensure_assistant_prefix(log)
            self._safe_write(log, event.text or "")

        elif event.kind == "assistant_text_delta":
            self._clear_thinking()
            self._ensure_assistant_prefix(log)
            self._stream_buffer += event.text or ""
            self._stream_flush_lines(log)

        elif event.kind == "assistant_text_end":
            self._stream_flush_all(log)

        elif event.kind == "assistant_thinking":
            if self.app._log_model_thinking:
                self._clear_thinking()
                log.write(f"[dim]{event.text}[/dim]")

        elif event.kind == "assistant_use_tool":
            self._clear_thinking()
            input_dict = event.tool_input or {}
            details = _format_tool_details(event.tool_name, input_dict)
            detail_str = f" [dim italic]· {escape_markup(details)}[/dim italic]" if details else ""
            log.write(
                f"[dim] Waiting on tool: [/dim][cyan]{event.tool_name}[/cyan][dim]…[/dim]{detail_str}"
            )
            if not event.expects_response:
                # Server-side / provider-managed tool — display only.
                return

            # UI-side tool execution. Hits the local tool server and feeds the
            # result back through the backend.
            payload = dict(input_dict)
            payload["tool_name"] = TOOL_NAME_MAP.get(event.tool_name, event.tool_name)
            try:
                response = requests.post(API_URL, json=payload, timeout=10)
                result = response.json() if response.ok else {"error": response.text}
            except Exception as exc:
                result = {"error": str(exc)}

            response_details = _format_tool_response(event.tool_name, result)
            if response_details:
                log.write(
                    f"[dim] Tool result: [/dim][green]{event.tool_name}[/green][dim italic] · {escape_markup(response_details)}[/dim italic]"
                )
            try:
                backend.submit(BackendEvent(
                    kind="tool_send_response",
                    tool_use_id=event.tool_use_id,
                    tool_result=result,
                ))
            except Exception as exc:
                log.write(f"[red]tool result send failed:[/red] {exc}")

        elif event.kind == "turn_end":
            self._stream_flush_all(log)
            if event.stop_reason == "end_turn":
                self._assistant_prefixed = False
                self._is_idle = True
                if self._pending_user_messages:
                    next_text = self._pending_user_messages.pop(0)
                    self._dispatch_user_message(next_text)
                else:
                    log.write("[dim]_____________________[/dim]")
                    log.write("[bold green]Aime ready[/bold green]")

        elif event.kind == "session_terminated":
            log.write("[red]session terminated[/red]")

        elif event.kind == "error":
            log.write(f"[red]backend error:[/red] {event.error}")

if __name__ == "__main__":
    Aime().run()
