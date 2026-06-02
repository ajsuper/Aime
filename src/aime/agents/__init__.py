"""Background agents: headless workers that reuse the full Aime stack to carry
out a task against a user's database and return a result.

A background agent is just an ``AgentSpec`` (a task brief plus a few knobs).
``BackgroundAgentRunner`` runs one by standing up the same backend, controller,
and tool gateway an interactive chat uses — in headless mode — kicking the task
off as a system message and collecting the structured result the worker returns
via its SubmitResult tool. Runs are persisted (encrypted, per user) by
``AgentRunStore``, separate from the user's conversations.

Build a new agent by declaring a spec and registering it:

    from aime.agents import AgentSpec, register

    register(AgentSpec(
        name="calendar-auditor",
        description="Flags events that look stale or mislabeled.",
        instructions="Review this month's events and report any that ...",
    ))

Then run it:

    from aime.agents import BackgroundAgentRunner, get
    runner = BackgroundAgentRunner()
    result = runner.run(get("calendar-auditor"), user_id=1, dek=dek, runs_dir=...)

This module ships no concrete agents — only the framework.
"""

from .spec import AgentSpec, AgentResult, RunStatus
from .registry import register, get, all_specs
from .runner import BackgroundAgentRunner
from .store import AgentRunStore, new_run_id
from .collector import ResultCollector

__all__ = [
    "AgentSpec",
    "AgentResult",
    "RunStatus",
    "register",
    "get",
    "all_specs",
    "BackgroundAgentRunner",
    "AgentRunStore",
    "new_run_id",
    "ResultCollector",
]
