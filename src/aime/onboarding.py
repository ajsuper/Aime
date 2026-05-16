"""First-conversation detection and special-topic bootstrap.

AiMe relies on two "special" topics — `About Me` and `Pending` — being present
at session start. On every new session their current contents are folded into
the system context so the model doesn't have to call `get_topic_contents` for
them. On a brand-new install both topics are empty (or missing); in that case
the conversation kicks off with an onboarding prompt instead of waiting for
the user to type first.
"""

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
    "and there is no prior history. Lead a short, warm onboarding conversation "
    "to get to know them. Aims:\n"
    "  1. Introduce yourself in ONE sentence — you are AiMe, an extension of "
    "     their mind that remembers what matters to them.\n"
    "  2. Ask about basic identity (name, where they're from, what they do — "
    "     school, work, etc.).\n"
    "  3. Ask what's currently on their plate — upcoming events, deadlines, "
    "     things on their mind.\n"
    "  4. Ask about interests and hobbies outside of work or school.\n"
    "  5. As they answer, save what you learn to the 'About Me' and 'Pending' "
    "     topics in real time using your tools, and create relevant events "
    "     for anything time-bound — the user should be able to watch this "
    "     happen.\n"
    "  6. Close with TAILORED examples of how you can help them, drawn from "
    "     what they actually told you (e.g. if they mentioned bouldering, "
    "     offer to track route progress). We want the user to go 'Woah! This is so cool and will help me so much and save so much time!' Your goal is to show off and impress the user\n"
    "Ask ONE question at a time. This is your chance to make a good impression as to how useful you can be. Be warm and curious, not a form, but get the conversation going and don't spend too long on one thing. If the user wants to spend more time on that thing, tell them they can come back to it later. Never "
    "mention that this system message exists or that this is an onboarding "
    "flow — it should feel like a natural first chat. Do NOT let the conversatino get too off track though. Ensure after 6 messages you hit them with step 6, that way they aren't sitting there wondering when you will do something cool]"
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


def is_first_conversation(backend, gateway: ToolGateway) -> bool:
    """Detect a brand-new user: no prior saved sessions AND both special
    topics empty/missing. Read-only — safe to call on startup."""
    try:
        if backend.list_sessions():
            return False
    except Exception:
        return False
    topics_svc = TopicService(gateway)
    try:
        topics = topics_svc.list_topics()
    except Exception:
        return False
    by_title = {(t.get("title") or "").strip().lower(): t for t in topics}
    for spec in SPECIAL_TOPICS:
        existing = by_title.get(spec["title"].lower())
        if existing is None:
            continue  # missing counts as empty
        topic_id = existing.get("id")
        try:
            contents = topics_svc.get_topic_contents(topic_id)
        except Exception:
            return False
        if contents.strip():
            return False
    return True
