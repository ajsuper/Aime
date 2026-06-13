"""Walk a saved messages snapshot and emit a CoreEvent sequence that lets a
frontend repopulate its transcript on `/load`.

Best-effort reconstruction: real turns were not captured event-by-event, so
streaming deltas don't exist here — each assistant text block surfaces as a
single `assistant_text` event, and each tool_use surfaces as a `tool_call`
with no matching `tool_result` (the original tool result was a user-role
block, not a separate display event).
"""

from typing import Iterator

from provider_backend import RECOVERY_MARKER

from .controller import CoreEvent


def replay_messages(messages: list[dict]) -> Iterator[CoreEvent]:
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")
        if not isinstance(content, list):
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
                yield CoreEvent(
                    kind="notice", severity="recovery", from_replay=True,
                )
                continue
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
                    # CreateGraphics / GetGraphic are internal plumbing on replay:
                    # a graphic renders from the `[graphic-N]` tag the model wrote
                    # into its own assistant text (resolved against the stored
                    # asset), not from the tool_call, so don't surface either.
                    if block.get("name") in ("CreateGraphics", "GetGraphic"):
                        continue
                    yield CoreEvent(
                        kind="tool_call",
                        tool_name=block.get("name", "tool"),
                        from_replay=True,
                    )
