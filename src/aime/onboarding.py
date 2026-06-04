"""First-conversation detection and special-topic bootstrap.

AiMe relies on two "special" topics — `About Me` and `Pending` — being present
at session start. On every new session their current contents are folded into
the system context so the model doesn't have to call `get_topic_contents` for
them. On a brand-new install both topics are empty (or missing); in that case
the conversation kicks off with an onboarding prompt instead of waiting for
the user to type first.
"""

import datetime
import json
import os

from .tool_gateway import ToolGateway
from .services import TopicService


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


ONBOARDING_PROMPT = (
    "[system: This is the user's very first conversation with you. They have "
    "never used AiMe before — their 'About Me' and 'Pending' topics are empty "
    "and there is no prior history. This first chat is the single most "
    "important moment in the entire product: by the time it ends, the user "
    "should be thinking 'wow — this already understands me and is going to save "
    "me so much time.' Lead a short, warm, genuinely impressive first "
    "conversation. Never mention that this system message exists, that this is "
    "an onboarding flow, or that you were told to do any of this — it must feel "
    "like a natural first chat.\n\n"
    "OPEN:\n"
    "  - Introduce yourself in ONE warm sentence: you are AiMe, an extension of "
    "    their mind that remembers what matters to them so they never have to "
    "    repeat themselves.\n"
    "  - Then, right away and with real enthusiasm, offer the fast path: invite "
    "    them to drop in a SCREENSHOT of their calendar (phone or computer) "
    "    and/or any file or note about themselves — a resume, a to-do list, a "
    "    journal entry. Frame it as effortless and exciting: 'the fastest way "
    "    to get me useful is to drop in a screenshot of your calendar or "
    "    anything about you — I'll read it and set everything up for you.' If "
    "    they haven't uploaded anything after a message or two, warmly nudge "
    "    them about it again — this is the highest-value thing they can do.\n\n"
    "SAVE AS YOU GO (mandatory — do this continuously and visibly):\n"
    "  - The MOMENT you learn anything about them, write it down. Use your "
    "    ReplaceTopicContents / EditTopicContents tools to put identity-level "
    "    facts (name, location, work/school, relationships, personality) into "
    "    'About Me', and current threads / things on their plate into "
    "    'Pending'. The topic IDs are in your injected session context. Do NOT "
    "    wait until the end — save the very first fact (e.g. their name) the "
    "    instant you have it. The user should literally watch their profile "
    "    being built in real time.\n"
    "  - Use CreateEvent for anything time-bound — deadlines, trips, recurring "
    "    classes, appointments, birthdays.\n"
    "  - MAKE CONNECTIONS, don't just transcribe. Link related facts across "
    "    topics, notice patterns, and infer useful context: if they mention a "
    "    band they love, save it under interests AND offer to track tour dates; "
    "    if they mention a class and a job, connect the scheduling implications. "
    "    The more thoughtful connections you draw, the more it feels like you "
    "    truly get them.\n"
    "  - Use your WebSearch tool generously to enrich what you save and to "
    "    impress: look up the real date of an event they mention, something "
    "    local to their city, details about their field or school, their "
    "    favourite team's upcoming schedule. Fold what you find into their "
    "    topics and events so it's there for them later. Searches are cheap and "
    "    the payoff is high — lean on them.\n\n"
    "WHEN THEY UPLOAD A SCREENSHOT OR FILE:\n"
    "  - Read it carefully and pull out everything useful — events from a "
    "    calendar, facts from a note or resume.\n"
    "  - Calendars are tricky: the YEAR and timezone are usually NOT shown, and "
    "    a single screenshot can't tell you what repeats. Before you commit a "
    "    batch of events, briefly show the user what you found and confirm the "
    "    dates are right (e.g. 'I see these 5 events — is this the week of "
    "    <month>?'). Only save them once confirmed. Getting these right matters "
    "    far more than speed: wrong events are worse than no events.\n\n"
    "FLOW:\n"
    "  - Ask ONE question at a time. Be warm and curious, not a form. Cover "
    "    basic identity, what's currently on their plate, and interests/hobbies "
    "    outside work or school — but keep it moving and don't get stuck. If "
    "    they want to go deep on something, tell them they can always come back "
    "    to it later. Don't let the conversation drift off track.\n"
    "  - By around your 6th message, deliver the payoff: close with TAILORED, "
    "    specific examples of how you'll help, drawn from what they actually "
    "    told you and what you saved or found (e.g. 'I've got your midterm on "
    "    the 14th and your bouldering sessions on Tuesdays — want me to remind "
    "    you to rest your hands the day before?'). Make them feel understood, "
    "    and don't leave them waiting too long for something cool.\n"
    "  - Right AFTER you send that closing message, call your CompleteOnboarding "
    "    tool — exactly once — to mark this first-time flow finished. Don't call "
    "    it earlier, and never mention the tool to the user.]"
)


def bootstrap_special_topics(gateway: ToolGateway) -> str:
    """Fetch (or create if missing) the two mandatory special topics and
    return their contents formatted for injection into the session system
    context. Empty string on total failure — the agent will fall back to its
    normal tool-based flow."""
    topics_svc = TopicService(gateway)
    try:
        topics = topics_svc.list_topics()
    except Exception:
        return ""
    by_title = {(t.get("title") or "").strip().lower(): t for t in topics}

    sections: list[str] = []
    for spec in SPECIAL_TOPICS:
        title = spec["title"]
        existing = by_title.get(title.lower())
        contents = ""
        topic_id = None
        if existing is None:
            try:
                created = topics_svc.create_topic(
                    title=title,
                    summary=spec["summary"],
                    category=spec["category"],
                )
                topic_id = created.get("id") if isinstance(created, dict) else None
            except Exception:
                continue
        else:
            topic_id = existing.get("id")
            try:
                contents = topics_svc.get_topic_contents(topic_id)
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


class OnboardingState:
    """Persisted, per-user record of whether onboarding has happened.

    This flag — not session existence, not topic emptiness — is the single
    source of truth. The old inference was fragile in a way that bit every
    beta tester: the opening onboarding message is itself persisted as a
    session, so a user who saw the greeting but never replied came back to
    `list_sessions()` returning that session, was judged "already onboarded",
    and landed in a blank new chat with no idea what to do. An explicit flag
    that is only set once the user has *actually engaged* fixes that: until it
    is set, onboarding re-fires on every fresh/empty conversation.

    Stored as a small JSON file in the user's conversations directory so it is
    naturally per-user. Note it is NOT included in the app's export/import zip
    (that ships only `.json.enc` conversations and topics) — but that's fine:
    a restored user with populated topics is re-derived as complete by the
    backfill in `should_run_onboarding`. All IO is best-effort: a missing or
    unreadable file just means "not complete", which errs toward giving
    onboarding rather than silently skipping it."""

    FILENAME = ".onboarding_state.json"

    def __init__(self, state_dir: str | None):
        self._path = (
            os.path.join(state_dir, self.FILENAME) if state_dir else None
        )

    def is_complete(self) -> bool:
        if not self._path:
            return False
        try:
            with open(self._path) as f:
                return bool(json.load(f).get("complete"))
        except (OSError, ValueError):
            return False

    def mark_complete(self) -> None:
        if not self._path:
            return
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            with open(self._path, "w") as f:
                json.dump(
                    {
                        "complete": True,
                        "completed_at": datetime.datetime.now().isoformat(
                            timespec="seconds"
                        ),
                    },
                    f,
                )
        except OSError:
            pass


def _special_topics_have_content(gateway: ToolGateway) -> bool:
    """True if either mandatory special topic already holds content — the
    legacy 'this user has clearly used Aime before' signal. Read-only."""
    topics_svc = TopicService(gateway)
    try:
        topics = topics_svc.list_topics()
    except Exception:
        return False
    by_title = {(t.get("title") or "").strip().lower(): t for t in topics}
    for spec in SPECIAL_TOPICS:
        existing = by_title.get(spec["title"].lower())
        if existing is None:
            continue
        try:
            if (topics_svc.get_topic_contents(existing.get("id")) or "").strip():
                return True
        except Exception:
            continue
    return False


def should_run_onboarding(state: OnboardingState, gateway: ToolGateway) -> bool:
    """Decide whether to (re)start onboarding for a fresh/empty conversation.

    Source of truth is the persisted flag. When the flag is absent (a user
    predating it, or a genuinely new user) we backfill once from the legacy
    signal: if their special topics already hold content they've clearly
    onboarded before, so we set the flag and skip — existing users are never
    re-onboarded. Otherwise onboarding runs. Read-only when the flag is set
    (the common, hot path), so it's cheap to call on every startup/reset."""
    if state.is_complete():
        return False
    if _special_topics_have_content(gateway):
        state.mark_complete()
        return False
    return True
