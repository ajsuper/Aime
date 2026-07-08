"""Walk a saved messages snapshot and emit a CoreEvent sequence that lets a
frontend repopulate its transcript on `/load`.

Best-effort reconstruction: real turns were not captured event-by-event, so
streaming deltas don't exist here — each assistant text block surfaces as a
single `assistant_text` event, and each tool_use surfaces as a `tool_call`
with no matching `tool_result` (the original tool result was a user-role
block, not a separate display event).
"""

import re
from typing import Iterator

from provider_backend import RECOVERY_MARKER, PROACTIVE_TRIGGER_MARKER

from .controller import CoreEvent


# Hidden out-of-band context the controller prepends to a user message before
# sending it to the model (current active events, records that changed mid-turn,
# the volatile clock). It's for the model, never the user — strip it from the
# replayed bubble so a resumed/loaded thread shows only what the user actually
# typed, not the raw <active_events>/<stale>/<clock> tags.
_HIDDEN_PREFIX_RE = re.compile(
    r"\A\s*(?:"
    r"<active_events>.*?</active_events>"
    r"|<stale>.*?</stale>"
    r"|<clock\b[^>]*>.*?</clock>"
    r")\s*",
    re.DOTALL,
)


def _strip_hidden_prefix(text: str) -> str:
    """Remove any run of leading hidden-context blocks from a stored user
    message, leaving just the user's own words."""
    prev = None
    while prev != text:
        prev = text
        text = _HIDDEN_PREFIX_RE.sub("", text, count=1)
    return text


def replay_messages(messages: list[dict]) -> Iterator[CoreEvent]:
    # Tracks whether the turn we just passed was the hidden trigger that precedes a
    # proactive (out-of-band) assistant message, so we can replay that assistant
    # turn as a `proactive_message` — preserving its identity so the frontend can
    # decide whether it's still "New" — rather than as an ordinary `assistant_text`.
    prev_proactive_trigger = False
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")
        if not isinstance(content, list):
            prev_proactive_trigger = False
            continue
        if role == "user":
            # A recovery-flattened message holds a condensed transcript meant
            # for the model, not for display — surface it as a short recovery
            # notice rather than a giant verbatim bubble.
            first_text = next(
                (b.get("text", "") for b in content
                 if isinstance(b, dict) and b.get("type") == "text"),
                "",
            )
            if first_text.startswith(RECOVERY_MARKER):
                prev_proactive_trigger = False
                yield CoreEvent(
                    kind="notice", severity="recovery", from_replay=True,
                )
                continue
            # Hidden trigger turn we slip in ahead of a proactive assistant
            # message (see append_assistant_message). It exists only to keep the
            # API history valid; the user never sent it, so don't render it — but
            # remember it so the assistant turn that follows replays as proactive.
            if first_text.startswith(PROACTIVE_TRIGGER_MARKER):
                prev_proactive_trigger = True
                continue
            prev_proactive_trigger = False
            # A user message can mix one or more text blocks with image blocks.
            # Collapse them into a single user_message_shown event so the
            # frontend can render attachments in the same bubble as the text.
            text_parts: list[str] = []
            attachments: list[dict] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    text = block.get("text", "")
                    marker = "[End System Info]"
                    if marker in text:
                        text = text.split(marker, 1)[1].strip()
                    # Drop the hidden model-only prefix (active events / stale /
                    # clock) so the replayed bubble shows only the user's words.
                    text = _strip_hidden_prefix(text)
                    if text:
                        text_parts.append(text)
                elif btype == "image":
                    src = block.get("source") or {}
                    if src.get("type") == "base64":
                        attachments.append({
                            "kind": "image",
                            "media_type": src.get("media_type") or "image/png",
                            "data": src.get("data") or "",
                        })
            if text_parts or attachments:
                yield CoreEvent(
                    kind="user_message_shown",
                    text="\n\n".join(text_parts),
                    attachments=attachments,
                    from_replay=True,
                )
        elif role == "assistant":
            was_proactive = prev_proactive_trigger
            prev_proactive_trigger = False
            if was_proactive:
                # The out-of-band message itself: replay it as a proactive_message
                # (a single Aime bubble) so the frontend renders it identically to
                # a live push, carrying the stored id so it can tell whether this
                # message has already been seen (→ "New" only if not).
                body = "\n\n".join(
                    b.get("text", "") for b in content
                    if isinstance(b, dict) and b.get("type") == "text" and b.get("text")
                )
                if body:
                    yield CoreEvent(
                        kind="proactive_message", text=body, from_replay=True,
                        pid=msg.get("pid", ""),
                    )
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    text = block.get("text", "")
                    if text:
                        yield CoreEvent(
                            kind="assistant_text", text=text, from_replay=True
                        )
                elif btype in ("tool_use", "server_tool_use"):
                    # CreateGraphics / GetGraphic / LoadGraphicsExamples are
                    # internal plumbing on replay: a graphic renders from the
                    # `[graphic-N]` tag the model wrote into its own assistant text
                    # (resolved against the stored asset), not from the tool_call,
                    # and the examples tool is pure scaffolding — so surface none.
                    if block.get("name") in (
                            "CreateGraphics", "GetGraphic", "LoadGraphicsExamples"):
                        continue
                    yield CoreEvent(
                        kind="tool_call",
                        tool_name=block.get("name", "tool"),
                        from_replay=True,
                    )
