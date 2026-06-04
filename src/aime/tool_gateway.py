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
        return result

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
