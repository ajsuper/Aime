"""Thin client for the local tool server (the HTTP endpoint that fronts
events/topics persistence).

Every tool call in Aime — whether triggered by the agent or by a view widget
asking "list this month's events" — goes through here. Centralizing it means
the rest of the codebase never imports `requests` and never references
`API_URL` directly.
"""

import datetime
import zoneinfo
from typing import Callable

import requests

from .tool_formatting import TOOL_NAME_MAP
from .config import API_URL
from .event_length import normalize_event_length, EventLengthError
from .weekday_check import check_weekday, WeekdayError
from . import freshness as _freshness
from . import datesidecar as _datesidecar
from . import topic_refs as _topic_refs


# Backend tools that accept an event-length `duration` sugar we resolve to an
# absolute end before forwarding (the backend only ever stores end_date/end_time).
_EVENT_WRITE_TOOLS = frozenset({"create_event", "replace_event"})

# Backend tools that write free-form topic prose. These get a `stamp_date`
# so the backend can record which sections changed (see aime.freshness), and
# have any annotation the model copied out of its own context stripped back
# out first.
_TOPIC_WRITE_TOOLS = frozenset({"replace_topic_contents", "edit_topic_contents"})


# Backend tool-name prefixes that only read state. Anything not matching is
# treated as a mutation so that newly added mutating tools auto-trigger a
# cross-session refresh without anyone remembering to wire it up.
_READONLY_PREFIXES = ("get_", "list_")
# Specific read-only backend tools that don't match the prefix rule.
_READONLY_TOOLS = frozenset({"reload_database"})


def _is_mutation(backend_tool_name: str) -> bool:
    if backend_tool_name in _READONLY_TOOLS:
        return False
    return not backend_tool_name.startswith(_READONLY_PREFIXES)


class ToolGateway:
    def __init__(
        self,
        api_url: str = API_URL,
        timeout: float = 10.0,
        user_id: int | None = None,
        on_mutation: Callable[[str, dict], None] | None = None,
    ):
        """`user_id` is forwarded with every backend call so the C++ side can
        route each request to that user's database. None preserves the legacy
        single-DB behavior the backend still uses today; the field is just
        absent in that case.

        `on_mutation` fires after any successful call whose tool name isn't
        read-only, receiving `(backend_tool_name, request_body)` — the body
        carries the record id, so the handler can tell *what* changed and fan a
        precise refresh/stale notification out from this one choke point. Hooking
        it here (rather than at each endpoint) means a future mutating tool ships
        sync support automatically."""
        self._url = api_url
        self._timeout = timeout
        self._user_id = user_id
        self._on_mutation = on_mutation
        # IANA timezone of the user, set per session (see set_client_timezone).
        # Drives the "now" stamped onto get_events so the backend reconciles
        # stale past events against the user's local clock, not the server's.
        self._client_tz: str | None = None

    def set_client_timezone(self, tz: str | None) -> None:
        """Record the user's IANA timezone (e.g. 'America/New_York'). Forwarded
        here by the controller so reads can carry a user-local 'now'."""
        self._client_tz = tz or None

    def _now_local(self) -> datetime.datetime:
        """Current time in the user's timezone, falling back to the server's
        local time when no (or an invalid) timezone has been set."""
        if self._client_tz:
            try:
                return datetime.datetime.now(zoneinfo.ZoneInfo(self._client_tz))
            except Exception:
                pass
        return datetime.datetime.now()

    def _post(self, body: dict) -> dict:
        if body.get("tool_name") in _EVENT_WRITE_TOOLS:
            # Verify the asserted `weekday` against `date` and consume it. The
            # model derives the two independently, so a slip in its "next
            # Tuesday" arithmetic makes them disagree — the one mechanically
            # detectable signature of a date that is well-formed but wrong.
            # Checked before the length work: a payload whose day-of-week is
            # wrong is suspect whatever its end looks like.
            try:
                body = check_weekday(body)
            except WeekdayError as exc:
                return {"error": str(exc)}
            # Resolve a `duration` sugar (or validate an explicit end) into the
            # absolute end_date/end_time the backend stores. Done here, the one
            # choke point both the agent (execute) and UI (call) paths share, so
            # neither can write an event with an unresolved or inverted end. A
            # bad combination is reported back to the caller, never sent to C++.
            try:
                body = normalize_event_length(body)
            except EventLengthError as exc:
                return {"error": str(exc)}
        if body.get("tool_name") in _TOPIC_WRITE_TOOLS:
            body = self._prepare_topic_write(body)
        if self._user_id is not None:
            body["user_id"] = self._user_id
        # Stamp every events read with the user-local date/time so the backend
        # can sweep elapsed `scheduled` events to `unknown` (see serve.cpp's
        # reconcileStalePastEvents). Callers may override by pre-setting these.
        if body.get("tool_name") == "get_events" and "now_date" not in body:
            now = self._now_local()
            body["now_date"] = now.strftime("%d/%m/%Y")
            body["now_time"] = now.strftime("%H:%M")
        try:
            response = requests.post(self._url, json=body, timeout=self._timeout)
        except Exception as exc:
            return {"error": str(exc)}
        if not response.ok:
            return {"error": response.text}
        try:
            result = response.json()
        except ValueError as exc:
            return {"error": f"invalid JSON from tool server: {exc}"}
        if (
            self._on_mutation
            and not (isinstance(result, dict) and "error" in result)
            and _is_mutation(body.get("tool_name", ""))
        ):
            try:
                self._on_mutation(body.get("tool_name", ""), body)
            except Exception:
                pass
        if body.get("tool_name") == "get_topic_contents":
            result = self._annotate_topic_read(result)
        return result


    def _prepare_topic_write(self, body: dict) -> dict:
        """Strip our own annotations out of a topic write and stamp it with the
        user-local date.

        The model is handed `<freshness>` and `<dates>` blocks on every topic
        read. Both are machine-generated, so removing them again is exact
        matching — no natural-language guesswork. Without this, the model
        eventually copies its own context into a write and an annotation gets
        persisted as though it were content the user wrote.

        Patch fragments are stripped too: a `find` carrying an annotation could
        never match stored content, and a `replace` carrying one would write it
        straight into the file.
        """
        out = dict(body)

        def clean(text):
            if not isinstance(text, str):
                return text
            return _topic_refs.strip(_datesidecar.strip(_freshness.strip(text)))

        if "contents" in out:
            out["contents"] = clean(out["contents"])
        patches = out.get("patches")
        if isinstance(patches, list):
            out["patches"] = [
                {**p, "find": clean(p.get("find")), "replace": clean(p.get("replace"))}
                if isinstance(p, dict) else p
                for p in patches
            ]
        # The backend does no date math — it stamps the string we hand it, in
        # the user's timezone, exactly as get_events is handed its "now".
        out.setdefault("stamp_date", self._now_local().strftime("%Y-%m-%d"))
        return out

    def _annotate_topic_read(self, result):
        """Attach the freshness and date sidecars to a topic read.

        Both go in a private `_annotations` key rather than into `contents`,
        for two reasons that run through this whole feature: `contents` must
        stay byte-identical to what is stored or the model's find/replace
        patches stop matching, and the web UI renders `contents` directly, where
        model-facing metadata has no business appearing.

        Best-effort — an annotation is never worth failing a read over.
        """
        if not isinstance(result, dict) or "error" in result:
            return result
        raw = result.get("contents")
        if not isinstance(raw, str):
            return result
        out = dict(result)
        try:
            today = self._now_local().date()
            body, _ = _freshness.parse(raw)
            # The stored block is bookkeeping; nobody downstream should see it.
            out["contents"] = body
            parts = [p for p in (_freshness.sidecar(raw, today),
                                 _datesidecar.sidecar(body, today),
                                 self._resolve_event_refs(body, today)) if p]
            out["_annotations"] = "\n\n".join(parts)
        except Exception:
            return result
        return out

    def _resolve_event_refs(self, body: str, today):
        """Resolve any `[event-N]` references in `body` against the live
        calendar.

        Costs one extra backend read, and only when the topic actually contains
        a reference — the overwhelmingly common case is none, which costs
        nothing. `get_events` has no id filter, so this pulls the user's events
        and looks the ids up locally, the same approach active_events takes.

        Best-effort: a failure here drops the annotation rather than failing
        the topic read.
        """
        if not _topic_refs.event_ids(body):
            return ""
        try:
            data = self.call("get_events", archived="all")
            if isinstance(data, dict):
                if data.get("error"):
                    return ""
                data = data.get("events") or []
            if not isinstance(data, list):
                return ""
            events = {}
            for ev in data:
                if isinstance(ev, dict) and ev.get("id") is not None:
                    try:
                        events[int(ev["id"])] = ev
                    except (TypeError, ValueError):
                        continue
            return _topic_refs.sidecar(body, events, today)
        except Exception:
            return ""

    def execute(self, agent_tool_name: str | None, tool_input: dict) -> dict:
        """Run a tool by its agent-side name (translated to the backend name
        via TOOL_NAME_MAP). Returns the parsed JSON response, or
        `{"error": "..."}` on transport/HTTP failure.

        `agent_tool_name=None` is treated as a no-op error so the agent loop
        can still surface a tool_result and keep moving."""
        if not agent_tool_name:
            return {"error": "missing tool name"}
        payload = dict(tool_input or {})
        payload["tool_name"] = TOOL_NAME_MAP.get(agent_tool_name, agent_tool_name)
        return self._post(payload)

    def call(self, backend_tool_name: str, **payload) -> dict:
        """Direct call by backend tool name (no TOOL_NAME_MAP translation).
        Used by view-side services that already speak the backend vocabulary
        (e.g. `get_events`, `get_topics`)."""
        body = dict(payload)
        body["tool_name"] = backend_tool_name
        return self._post(body)
