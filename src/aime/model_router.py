"""Per-turn model routing.

A cheap Haiku classifier call picks between Haiku and Sonnet for each user
turn. Haiku handles read-only lookups ("what do I have this week?", "when is
X?", "what's in topic Y?"); Sonnet handles anything that creates or edits
state (events, topics, folders, contents) or that needs multi-step reasoning.

Design constraints (set by the operator, not the model):
  * Lean toward Haiku on read-only turns. The bright line is mutation:
    anything that creates/edits/deletes/moves state, does real multi-step
    planning, or writes prose goes to Sonnet. Pure lookups go to Haiku, and
    a read-only turn that needs only light reasoning over a few items can go
    to Haiku too. Reserve Sonnet for mutation, genuine planning, and prose.
  * Continuation turns inside a tool loop stay on the model that started the
    turn. Downgrading mid-loop strands tool_use blocks the cheap model didn't
    plan for, and prompt-cache reads change rate.
  * Image-bearing turns always go to Sonnet — Haiku is allowed for them but
    in this app they almost always accompany a mutating request.
  * Any classifier failure (network, parse, timeout) falls through to Sonnet.
    The router must never block or break a turn.
"""

import threading
from typing import Callable

from anthropic import Anthropic


_CLASSIFIER_SYSTEM = (
    "You are a routing classifier for Aime, a personal-assistant chat that "
    "manages a user's calendar (events) and a knowledge base (topics organised "
    "into folders). Read the user's latest message and decide whether the "
    "next assistant turn is EASY or HARD. Output exactly one token: EASY or "
    "HARD. Nothing else.\n\n"
    "EASY — route to a cheaper, smaller model. Use whenever the request is "
    "read-only — answerable by fetching data and reading it back — with NO "
    "mutation, no real multi-step planning, and no prose writing. Light "
    "reasoning over a handful of items is fine and still EASY. EASY cases "
    "include:\n"
    "  - \"what do I have this week / today / on Friday?\"\n"
    "  - \"when is <event>?\" / \"do I have anything tomorrow morning?\"\n"
    "  - \"what's in our <topic> topic?\" / \"what does my About Me say?\"\n"
    "  - listing folders, listing topics, listing events in a date range\n"
    "  - short factual recall from already-fetched context\n"
    "  - a quick availability check that only scans a day or two "
    "(\"do I have time for lunch tomorrow?\")\n"
    "  - a brief recap of context already in the conversation\n"
    "  - greetings, thanks, yes/no acknowledgements with no follow-up action\n"
    "  - casual chat with no action required\n\n"
    "HARD — route to the stronger model. Use for ANYTHING that might:\n"
    "  - create, edit, replace, delete, rename, or move an event, topic, "
    "folder, or topic contents (even if the user phrases it as a question)\n"
    "  - schedule, reschedule, or shift something on the calendar\n"
    "  - plan a day / week / trip, suggest options, compare alternatives\n"
    "  - summarize, synthesize, or reason across many items\n"
    "  - resolve ambiguity, infer the user's intent, or need to ask for "
    "confirmation\n"
    "  - involve code, math beyond trivial arithmetic, or writing prose\n"
    "  - touch images, attachments, or anything besides plain conversational "
    "text\n\n"
    "Worked examples — study these and apply the same judgement to the "
    "incoming message.\n\n"
    "Example 1\n"
    "  User: \"what do I have on thursday\"\n"
    "  Decision: EASY  (pure read-only calendar lookup, no mutation)\n\n"
    "Example 2\n"
    "  User: \"when is my dentist appointment?\"\n"
    "  Decision: EASY  (single-event lookup by name)\n\n"
    "Example 3\n"
    "  User: \"what does our project-alpha topic say about the launch date?\"\n"
    "  Decision: EASY  (read one topic, recite a fact)\n\n"
    "Example 4\n"
    "  User: \"list my work folder\"\n"
    "  Decision: EASY  (enumeration of an existing folder)\n\n"
    "Example 5\n"
    "  User: \"hey, thanks!\"\n"
    "  Decision: EASY  (acknowledgement, no action)\n\n"
    "Example 6\n"
    "  User: \"add a dentist appointment for next tuesday at 3pm\"\n"
    "  Decision: HARD  (creates a calendar event — mutating action)\n\n"
    "Example 7\n"
    "  User: \"move my 10am meeting to thursday\"\n"
    "  Decision: HARD  (edits an existing event — mutating action)\n\n"
    "Example 8\n"
    "  User: \"can you add a note to the project-alpha topic that the launch "
    "slipped to may?\"\n"
    "  Decision: HARD  (edits topic contents — mutating action)\n\n"
    "Example 9\n"
    "  User: \"plan my week — i have to prep for the friday demo and finish "
    "the report\"\n"
    "  Decision: HARD  (multi-step planning across calendar and topics)\n\n"
    "Example 10\n"
    "  User: \"what's the best time this week to schedule a 2-hour focus "
    "block?\"\n"
    "  Decision: HARD  (reasoning over many calendar entries to pick a slot)\n\n"
    "Example 11\n"
    "  User: \"summarize what we discussed about the q3 roadmap\"\n"
    "  Decision: HARD  (summary across multiple items)\n\n"
    "Example 12\n"
    "  User: \"rename the 'work' folder to 'job'\"\n"
    "  Decision: HARD  (folder rename — mutating action)\n\n"
    "Example 13\n"
    "  User: \"do I have time for lunch with sarah next week?\"\n"
    "  Decision: HARD  (requires reasoning over availability, not a "
    "single-event lookup)\n\n"
    "Example 14\n"
    "  User: \"what topics do I have in my 'business' folder?\"\n"
    "  Decision: EASY  (lists topics inside one folder)\n\n"
    "Example 15\n"
    "  User: \"draft an email to my landlord about the broken sink\"\n"
    "  Decision: HARD  (writing prose)\n\n"
    "Example 16\n"
    "  User: \"is there anything on my calendar tonight?\"\n"
    "  Decision: EASY  (single-day calendar lookup)\n\n"
    "Example 17\n"
    "  User: \"what folders do I have?\"\n"
    "  Decision: EASY  (enumeration only)\n\n"
    "Example 18\n"
    "  User: \"delete the 'old-stuff' folder\"\n"
    "  Decision: HARD  (destructive mutation)\n\n"
    "Example 19\n"
    "  User: \"cancel my 2pm tomorrow and email Sam to apologise\"\n"
    "  Decision: HARD  (multi-step: event cancel + prose generation)\n\n"
    "Example 20\n"
    "  User: \"what's in my About Me?\"\n"
    "  Decision: EASY  (recite contents of one topic)\n\n"
    "Heuristic shortcuts:\n"
    "  - If the sentence contains a verb like add / create / make / schedule "
    "/ book / move / reschedule / shift / cancel / delete / remove / rename "
    "/ edit / update / set / change → almost always HARD.\n"
    "  - If the sentence is a wh- question (what / when / where / who / "
    "which / how many) and asks about ONE specific item or a single-day / "
    "single-folder listing → almost always EASY.\n"
    "  - If the sentence asks for a multi-step plan, draft/prose, or a "
    "synthesis across many items → HARD.\n"
    "  - If you are uncertain whether the turn mutates state, output HARD. "
    "If you are confident it is read-only but unsure how much reasoning it "
    "needs, prefer EASY.\n\n"
    "Decision rule: the bright line is mutation. Mutating, real multi-step "
    "planning, and prose writing are HARD; everything else — lookups and "
    "read-only turns that need only light reasoning — is EASY. Lean EASY on "
    "read-only turns. Output exactly one token: EASY or HARD. Do not output "
    "any other text, punctuation, or explanation."
)


class ModelRouter:
    """Decides which model handles each turn.

    Thread-safety: `choose()` is called from the backend's turn thread and may
    overlap with itself across sessions on the web frontend. The Anthropic
    client is itself thread-safe; the small bit of mutable state (`_sticky`)
    is guarded by a lock.
    """

    def __init__(
        self,
        *,
        haiku_model: str,
        sonnet_model: str,
        router_model: str,
        enabled: bool = True,
        usage_label: str | None = None,
        record_api: Callable | None = None,
    ):
        self._haiku = haiku_model
        self._sonnet = sonnet_model
        self._router_model = router_model
        self._enabled = enabled
        self._usage_label = usage_label
        # Injected so the router doesn't import aime.usage directly (keeps the
        # module reusable from tests and avoids an import cycle).
        self._record_api = record_api
        self._client = Anthropic(max_retries=1)
        self._lock = threading.Lock()

    def is_enabled(self) -> bool:
        return self._enabled

    def labels(self) -> tuple[str, str]:
        """(haiku_id, sonnet_id) — handy for the dashboard/backend to look up
        either pole without re-reading config."""
        return self._haiku, self._sonnet

    def choose(
        self,
        messages: list[dict],
        *,
        is_continuation: bool = False,
        has_images: bool = False,
        session_id: str | None = None,
    ) -> tuple[str, str]:
        """Pick a model for the next turn.

        Returns ``(api_model_id, label)`` where ``label`` is "haiku" or
        "sonnet". The label is what gets logged to the usage ledger and shown
        in verbose mode; the id is what's passed to ``messages.stream()``.

        Skips classification (always Sonnet) when routing is disabled, the
        turn is a continuation after tool_results, or any image is attached.
        """
        if not self._enabled or is_continuation or has_images:
            return self._sonnet, "sonnet"
        last_user_text = self._last_user_text(messages)
        if not last_user_text:
            return self._sonnet, "sonnet"
        try:
            decision = self._classify(last_user_text, session_id=session_id)
        except Exception:
            return self._sonnet, "sonnet"
        if decision == "haiku":
            return self._haiku, "haiku"
        return self._sonnet, "sonnet"

    # --- internals ---

    @staticmethod
    def _last_user_text(messages: list[dict]) -> str:
        """Concatenate the text blocks of the most recent user message. We
        only look at the last user turn — including history would balloon the
        classifier's prompt without improving accuracy on a one-line ask."""
        for msg in reversed(messages):
            if msg.get("role") != "user":
                continue
            content = msg.get("content")
            if isinstance(content, str):
                return content.strip()
            if not isinstance(content, list):
                continue
            parts: list[str] = []
            for blk in content:
                if not isinstance(blk, dict):
                    continue
                if blk.get("type") == "text":
                    txt = blk.get("text") or ""
                    if txt:
                        parts.append(txt)
                elif blk.get("type") == "tool_result":
                    # A user message that is *only* tool_results means we're
                    # mid-tool-loop — the caller passed `is_continuation`
                    # already, but guard here too so a stray call from
                    # elsewhere doesn't classify a tool_result block.
                    return ""
            if parts:
                return "\n\n".join(parts).strip()
            return ""
        return ""

    def _classify(self, user_text: str, *, session_id: str | None) -> str:
        # Cap the prompt so a long paste doesn't run up the classifier bill.
        snippet = user_text[:1500]
        resp = self._client.messages.create(
            model=self._router_model,
            # System prompt is byte-stable across every classifier call so it
            # is marked as a prompt-cache breakpoint with a 1h TTL. Anthropic
            # enforces a per-model minimum cacheable prefix length (Haiku is
            # currently ~1024 tokens); if the prompt is below that threshold
            # the cache_control hint is a silent no-op rather than an error,
            # so this is correct even while the prompt is short and will
            # start paying off automatically once it crosses the threshold.
            system=[{
                "type": "text",
                "text": _CLASSIFIER_SYSTEM,
                "cache_control": {"type": "ephemeral", "ttl": "1h"},
            }],
            messages=[{"role": "user", "content": snippet}],
            max_tokens=4,
        )
        if self._record_api is not None:
            try:
                self._record_api(
                    self._usage_label,
                    self._router_model,
                    getattr(resp, "usage", None),
                    purpose="route",
                    session_id=session_id,
                )
            except Exception:
                pass
        text = "".join(
            b.text for b in resp.content if getattr(b, "type", None) == "text"
        ).strip().upper()
        # Be generous about parsing — the model occasionally appends
        # punctuation or a stray word despite `max_tokens=4`.
        if text.startswith("EASY"):
            return "haiku"
        return "sonnet"
