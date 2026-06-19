"""``BackgroundAgentRunner`` — stands up the full Aime stack headlessly to run
one background-agent task and return its result.

A run reuses exactly the pieces an interactive session uses — an
``AnthropicMessagesBackend``, a ``ConversationController``, a ``ToolGateway``
bound to the user's id (which is what gives the worker database access), and an
optional offloaded ``WebSearchAgent`` — wired in headless mode:

  * the backend's system prompt is the headless base prompt, and its terminal
    tool is the agent's SubmitResult;
  * the backend persists nothing (the conversation is in-memory only);
  * the controller skips onboarding and arms SubmitResult;
  * a ``ResultCollector`` stands in for a frontend.

The runner kicks the task off as a system message, drives the run to a terminal
state (SubmitResult, error, exhausted budget, or stuck), persists a run record
via ``AgentRunStore``, and returns an ``AgentResult``.
"""

import datetime
import json
import threading

from provider_backend import AnthropicMessagesBackend, BackendEvent

from .. import config
from .. import messaging as _messaging
from .. import usage as _usage
from ..controller import ConversationController
from ..tool_gateway import ToolGateway
from ..web_search_agent import WebSearchAgent
from .collector import ResultCollector
from .spec import AgentResult, AgentSpec
from .store import AgentRunStore, new_run_id


# Sent when the worker ends a reply round without calling SubmitResult. There is
# no user to talk to, so this re-points it at the only valid way to finish.
_NUDGE = (
    "[system: You ended a reply without calling SubmitResult. There is no user "
    "reading that message. If the task is complete, call SubmitResult now with "
    "your summary (and structured result if the task asked for one). If it is "
    "not yet complete, keep working with your tools and then call SubmitResult.]"
)


def _utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


class BackgroundAgentRunner:
    """Runs background-agent tasks. Stateless across runs and safe to reuse;
    each ``run`` builds its own backend/controller and tears them down.

    Tuning:
      round_timeout_s: how long to wait for the worker to make progress (finish
                       a reply round or terminate) before declaring it stuck.
      max_nudges:      how many times to re-prompt a worker that goes idle
                       without submitting before giving up.
    """

    def __init__(self, *, round_timeout_s: float = 180.0, max_nudges: int = 2):
        self._round_timeout_s = round_timeout_s
        self._max_nudges = max_nudges

    def run(
        self,
        spec: AgentSpec,
        inputs: dict | None = None,
        *,
        user_id: int,
        dek: bytes,
        runs_dir: str,
        usage_label: str | None = None,
        client_tz: str | None = None,
        messaging_contact: str | None = None,
        api_url: str = config.API_URL,
        agent_id: str | None = None,
        quota=None,
    ) -> AgentResult:
        """Execute ``spec`` against ``user_id``'s database and return the result.

        ``runs_dir`` is the user's agent-runs directory (where the encrypted run
        record is written); ``dek`` is the user's data key, used both for that
        record and as the backend's (unused, since persistence is off) key.

        ``messaging_contact`` is the user's outbound-message destination
        (``UserRecord.messaging_contact``); when set, the worker can reach the
        user via the SendMessage tool or SubmitResult's ``message_to_user``
        field. The caller supplies it (the runner has no auth access of its own),
        which keeps contact resolution out of this layer.

        ``quota`` is the owning user's :class:`aime.quota.QuotaMeter` (or None
        when usage limits are disarmed). When present this run's real cost is
        debited from the user's budget, the same as an interactive turn — a run
        is never *blocked* here (on-demand runs are gated before launch; recurring
        ones are exempt by design), but its spend is accounted so an agent can't
        be a free channel around the budget. See docs/usage-limits.md.
        """
        run_id = new_run_id(spec.name)
        started_at = _utc_now_iso()

        backend, controller, collector = self._build(
            spec, user_id=user_id, dek=dek, runs_dir=runs_dir,
            usage_label=usage_label, api_url=api_url, client_tz=client_tz,
            messaging_contact=messaging_contact, quota=quota,
        )

        status = ""
        error: str | None = None
        try:
            controller.start()
            backend.submit(BackendEvent(
                kind="system_send_message", text=spec.render_kickoff(inputs),
            ))
            status, error = self._drive(spec, backend, collector)
        except Exception as exc:  # a framework failure must still return a result
            status, error = "error", f"{type(exc).__name__}: {exc}"
        finally:
            # Park any in-flight turn, then terminate the stream worker.
            try:
                controller.stop_model()
            except Exception:
                pass
            try:
                controller.shutdown()
            except Exception:
                pass

        result = AgentResult(
            status=status or "no_result",
            summary_text=collector.summary_text,
            result=collector.result_payload,
            run_id=run_id,
            agent_name=spec.name,
            turns=collector.idle_rounds + (1 if collector.completed else 0),
            error=error,
        )
        self._persist(
            run_id, spec, inputs, result, collector,
            started_at=started_at, dek=dek, runs_dir=runs_dir, user_id=user_id,
            agent_id=agent_id,
        )
        return result

    # --- internals ---

    def _build(
        self, spec, *, user_id, dek, runs_dir, usage_label, api_url, client_tz,
        messaging_contact=None, quota=None,
    ):
        web_search_agent = None
        web_search_schema = None
        if spec.web_search_allowed and config.WEB_SEARCH_ENABLED:
            web_search_agent = WebSearchAgent(
                model=config.WEB_SEARCH_MODEL,
                tool_version=config.WEB_SEARCH_TOOL_VERSION,
                usage_label=usage_label,
                record_api=_usage.record_api,
                # The offloaded search is real spend for the same user, so it
                # draws down the same budget as the run's own turns.
                quota_debit=(quota.debit if quota is not None else None),
                # Attribute this run's offloaded searches to agent usage, so
                # they land under the Agents tab rather than interactive
                # web_search. Cost is keyed to the owning user (usage_label).
                usage_source="agent",
            )
            web_search_schema = config.WEB_SEARCH_SCHEMA

        backend = AnthropicMessagesBackend(
            system_prompt=config.load_agent_base_prompt(),
            model=spec.model,
            schema_files=self._schema_files_for(spec),
            conversations_dir=runs_dir,   # required, but unused: persistence off
            dek=dek,
            usage_label=usage_label,
            # No router: a background run executes every turn on spec.model so
            # its behavior/cost are predictable, rather than being re-routed
            # between Haiku and Sonnet per turn.
            router=None,
            web_search_schema=web_search_schema,
            terminal_tool_schema=spec.submit_result_raw_schema(),
            persist_enabled=False,
            # Tag every API/tool record from this run as agent-sourced so the
            # usage dashboard can separate a user's agent cost from their
            # interactive cost. The owning user comes from usage_label.
            usage_source="agent",
            # Debit each turn's real cost from the user's budget (None disarms
            # it). The run is never blocked on the budget here — see the run()
            # docstring — but its spend is accounted.
            quota=quota,
        )
        backend.new_session()

        gateway = ToolGateway(api_url=api_url, user_id=user_id)

        def spawn_worker(fn):
            threading.Thread(
                target=fn, name=f"agent-run-{spec.name}", daemon=True
            ).start()

        # The messenger reflects *server* capability (is a channel configured?),
        # independent of whether this user connected a contact. Keeping them
        # separate lets the controller tell "messaging isn't set up on this
        # server" apart from "no contact connected for this user" — two
        # different, separately-actionable states.
        messenger = _messaging.get_messenger()

        controller = ConversationController(
            backend=backend,
            tool_gateway=gateway,
            worker_spawner=spawn_worker,
            web_search_agent=web_search_agent,
            headless=True,
            messenger=messenger,
            message_recipient=messaging_contact,
        )
        if client_tz:
            controller.set_client_timezone(client_tz)

        collector = ResultCollector()
        controller.subscribe(collector)
        return backend, controller, collector

    def _drive(self, spec, backend, collector) -> tuple[str, str | None]:
        """Block until the run reaches a terminal state, nudging a worker that
        goes idle without submitting. Returns (status, error)."""
        seen_rounds = 0
        nudges = 0
        while True:
            progressed = collector.wait_progress(seen_rounds, self._round_timeout_s)
            if not progressed:
                return "error", "the worker stopped responding (timed out)"
            if collector.done:
                if collector.completed:
                    return "completed", None
                if collector.error:
                    return "error", collector.error
                return "no_result", None
            # The worker finished a reply round without calling SubmitResult.
            seen_rounds = collector.idle_rounds
            if seen_rounds >= spec.max_turns:
                return "max_turns", None
            if nudges >= self._max_nudges:
                return "no_result", None
            nudges += 1
            backend.submit(BackendEvent(kind="system_send_message", text=_NUDGE))

    @staticmethod
    def _schema_files_for(spec: AgentSpec) -> list[str]:
        """The data-tool schema paths offered to this agent. The full Aime set
        unless the spec restricts it via ``tool_allowlist`` (matched against each
        schema's tool name / title). WebSearch and SubmitResult are handled
        separately and are not affected by the allowlist."""
        if spec.tool_allowlist is None:
            return config.SCHEMA_FILES
        keep: list[str] = []
        for path in config.SCHEMA_FILES:
            try:
                with open(path) as f:
                    title = json.load(f).get("title")
            except (OSError, ValueError):
                continue
            if title in spec.tool_allowlist:
                keep.append(path)
        return keep

    def _persist(
        self, run_id, spec, inputs, result, collector, *,
        started_at, dek, runs_dir, user_id, agent_id=None,
    ) -> None:
        record = {
            "run_id": run_id,
            "agent_name": spec.name,
            # The saved agent this run came from (None for ad-hoc runs), so the
            # dashboard can group a run under its agent's card.
            "agent_id": agent_id,
            "user_id": user_id,
            "status": result.status,
            "inputs": inputs or {},
            "summary": result.summary_text,
            "result": result.result,
            "error": result.error,
            "turns": result.turns,
            "started_at": started_at,
            "ended_at": _utc_now_iso(),
            "transcript": collector.transcript(),
        }
        AgentRunStore(runs_dir, dek).save(record)
