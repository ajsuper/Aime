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


# --- Agent permissions -------------------------------------------------------
# A background agent is least-privilege by default: it can always *read* the
# user's data, but each mutating (or side-effecting) capability is unlocked by
# an explicit permission toggle. The groups below name the tools each permission
# covers; ``permissions_to_allowlist`` turns a set of toggles into the
# ``tool_allowlist`` — the single source of truth for what an agent may do.
#
# Web search rides the same pipeline as every other capability: it is just
# another tool name ("WebSearch") in the allowlist. It differs only in *how* it
# is served (an offloaded Haiku sub-agent, not a SCHEMA_FILES data tool), so the
# runner reads ``AgentSpec.web_search_allowed`` to decide whether to wire that
# sub-agent in — but the gating decision lives in the allowlist, like the rest.

# Always available: every read-only data tool. An agent with no permissions can
# still look at events, topics, folders, and the activity/pattern summaries.
READONLY_TOOLS = frozenset({
    "FilterUsersEvents",
    "FilterTopics",
    "GetTopicContents",
    "ListFolders",
    "GetCommitmentHistory",
    "GetPatternSummary",
    "GetRecentActivity",
})

# Unlocked by the "modify events" permission.
MODIFY_EVENTS_TOOLS = frozenset({"CreateEvent", "EditEvent"})

# Unlocked by the "modify topics" permission (topic + folder writes).
MODIFY_TOPICS_TOOLS = frozenset({
    "CreateTopic",
    "ReplaceTopic",
    "ReplaceTopicContents",
    "EditTopicContents",
    "RenameFolder",
})

# Unlocked by the "send message" permission.
SEND_MESSAGE_TOOLS = frozenset({"SendMessage"})

# Unlocked by the "web search" permission. Not a SCHEMA_FILES tool (it's served
# by the offloaded Haiku sub-agent), but it lives in the allowlist all the same
# so every capability is gated in one place. The name matches the WebSearch tool
# the conversational model calls.
WEB_SEARCH_TOOLS = frozenset({"WebSearch"})


def permissions_to_allowlist(
    *,
    modify_topics: bool = False,
    modify_events: bool = False,
    send_message: bool = False,
    web_search: bool = False,
) -> frozenset[str]:
    """The ``tool_allowlist`` for an agent with the given permission toggles:
    the read-only baseline plus each unlocked group. Every capability — web
    search included — is expressed as a tool name here, so the allowlist is the
    one place that says what an agent may do."""
    allow = set(READONLY_TOOLS)
    if modify_events:
        allow |= MODIFY_EVENTS_TOOLS
    if modify_topics:
        allow |= MODIFY_TOPICS_TOOLS
    if send_message:
        allow |= SEND_MESSAGE_TOOLS
    if web_search:
        allow |= WEB_SEARCH_TOOLS
    return frozenset(allow)


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
                      (e.g. {"FilterUsersEvents", "GetTopicContents", "WebSearch"}).
                      None => the full Aime toolset (and web search). The single
                      source of truth for the worker's capabilities, including web
                      search (see ``web_search_allowed``). Build it from a set of
                      permission toggles with ``permissions_to_allowlist``.
      model:          Model id for the run. Defaults to the standard agent model.
      max_turns:      Safety budget: the maximum number of assistant turns before
                      the runner gives up on a worker that never calls
                      SubmitResult.
    """

    name: str
    description: str
    instructions: str
    result_schema: dict | None = None
    tool_allowlist: frozenset[str] | None = None
    model: str = config.AGENT_MODEL
    max_turns: int = 12

    @property
    def web_search_allowed(self) -> bool:
        """Whether the worker may use web search. Web search is gated by the
        allowlist like every other tool: present when "WebSearch" is allowed, or
        when the allowlist is None (the full toolset includes web search)."""
        return self.tool_allowlist is None or "WebSearch" in self.tool_allowlist

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
