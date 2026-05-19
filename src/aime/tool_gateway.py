"""Thin client for the local tool server (the HTTP endpoint that fronts
events/topics persistence).

Every tool call in Aime — whether triggered by the agent or by a view widget
asking "list this month's events" — goes through here. Centralizing it means
the rest of the codebase never imports `requests` and never references
`API_URL` directly.
"""

import requests

from .tool_formatting import TOOL_NAME_MAP
from .config import API_URL


class ToolGateway:
    def __init__(
        self,
        api_url: str = API_URL,
        timeout: float = 10.0,
        user_id: int | None = None,
    ):
        """`user_id` is forwarded with every backend call so the C++ side can
        route each request to that user's database. None preserves the legacy
        single-DB behavior the backend still uses today; the field is just
        absent in that case."""
        self._url = api_url
        self._timeout = timeout
        self._user_id = user_id

    def _post(self, body: dict) -> dict:
        if self._user_id is not None:
            body["user_id"] = self._user_id
        try:
            response = requests.post(self._url, json=body, timeout=self._timeout)
        except Exception as exc:
            return {"error": str(exc)}
        if not response.ok:
            return {"error": response.text}
        try:
            return response.json()
        except ValueError as exc:
            return {"error": f"invalid JSON from tool server: {exc}"}

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
