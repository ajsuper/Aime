"""The public surface of the background-agent framework: what an agent *is*
(``AgentSpec``) and what a run *produces* (``AgentResult``).

Building a new background agent is meant to be nothing more than filling in an
``AgentSpec`` and registering it (see ``aime.agents.registry``). All the
plumbing — backend, controller, tool gateway, web search, result collection,
persistence — is handled by ``BackgroundAgentRunner``; an agent author only
declares the task.
"""

import json
from dataclasses import dataclass
from typing import Literal

from .. import config


# The slot in SubmitResult's input schema whose shape a spec may override.
_RESULT_PROPERTY = "result"


@dataclass(frozen=True)
class AgentSpec:
    """The complete definition of a background agent.

    Everything the runner needs to stand up a headless worker and judge its
    output lives here, so an agent is a value, not a class to subclass.

    Fields:
      name:           Stable identifier, used for the registry and run records
                      (e.g. "calendar-auditor").
      description:    One line describing what the agent does. For humans and
                      for any future "pick an agent" UI; never sent to the model.
      instructions:   The task brief, delivered to the model as a system message
                      at kickoff. May contain ``{placeholders}`` filled from the
                      run's ``inputs`` (see ``render_kickoff``).
      result_schema:  Optional JSON-schema fragment describing the shape of the
                      ``result`` object the worker should return via SubmitResult.
                      When set, it replaces SubmitResult's generic ``result``
                      property so the model is guided to the right structure.
                      None => a free-form object (or omitted) result.
      tool_allowlist: Optional set of agent-facing tool names the worker may use
                      (e.g. {"FilterUsersEvents", "GetTopicContents"}). None =>
                      the full Aime toolset. Read-only agents can use this to
                      forbid mutations.
      model:          Model id for the run. Defaults to the standard agent model.
      max_turns:      Safety budget: the maximum number of assistant turns before
                      the runner gives up on a worker that never calls
                      SubmitResult.
      allow_web_search: Whether the worker is given the WebSearch tool.
    """

    name: str
    description: str
    instructions: str
    result_schema: dict | None = None
    tool_allowlist: frozenset[str] | None = None
    model: str = config.AGENT_MODEL
    max_turns: int = 12
    allow_web_search: bool = True

    def render_kickoff(self, inputs: dict | None = None) -> str:
        """The task message sent to the model at kickoff.

        ``instructions`` is formatted with ``inputs`` so a spec can be a
        template (e.g. "Audit the calendar for {month}"). Missing placeholders
        raise loudly at run time rather than silently producing a broken brief.
        The whole thing is wrapped in a ``[system: ...]`` marker, matching the
        convention the onboarding kickoff uses.
        """
        body = self.instructions
        if inputs:
            body = body.format(**inputs)
        return f"[system: background task]\n\n{body}"

    def submit_result_raw_schema(self) -> dict:
        """The raw JSON schema (title/description/type/properties) for this
        agent's SubmitResult tool, with the ``result`` property specialized to
        ``result_schema`` when one is given. Returned in the on-disk schema
        shape; the backend translates it into the Anthropic tool format."""
        with open(config.SUBMIT_RESULT_SCHEMA) as f:
            schema = json.load(f)
        if self.result_schema is not None:
            props = dict(schema.get("properties", {}))
            props[_RESULT_PROPERTY] = self.result_schema
            schema = {**schema, "properties": props}
        return schema


# Status of a finished run.
#   completed  — the worker called SubmitResult; `result`/`summary_text` are set.
#   max_turns  — the worker ran out of turn budget without submitting.
#   no_result  — the worker stopped (went idle / terminated) without submitting.
#   error      — an unrecoverable backend/controller error ended the run.
RunStatus = Literal["completed", "max_turns", "no_result", "error"]


@dataclass
class AgentResult:
    """What a background-agent run returns to its caller.

    ``result`` is the structured SubmitResult payload (the ``result`` field of
    the tool input) when the agent provided one; ``summary_text`` is always
    populated — from SubmitResult's ``summary`` on success, or the worker's last
    assistant text as a fallback — so a caller always has something human to
    show even on a partial run. The full transcript and timing live in the
    persisted run record, fetched from the store via ``run_id``.
    """

    status: RunStatus
    summary_text: str
    result: dict | None = None
    run_id: str = ""
    agent_name: str = ""
    turns: int = 0
    error: str | None = None
    usage: dict | None = None

    @property
    def ok(self) -> bool:
        return self.status == "completed"
