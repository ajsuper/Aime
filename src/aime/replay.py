"""Walk a saved messages snapshot and emit a CoreEvent sequence that lets a
frontend repopulate its transcript on `/load`.

Best-effort reconstruction: real turns were not captured event-by-event, so
streaming deltas don't exist here — each assistant text block surfaces as a
single `assistant_text` event, and each tool_use surfaces as a `tool_call`
with no matching `tool_result` (the original tool result was a user-role
block, not a separate display event).
"""

from typing import Iterator

from .controller import CoreEvent


def replay_messages(messages: list[dict]) -> Iterator[CoreEvent]:
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        if role == "user":
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") != "text":
                    continue
                text = block.get("text", "")
                # Strip the auto-injected "[System info] ... [End System Info]"
                # date prefix that the backend prepends — it's noise to the
                # user re-reading their own message.
                marker = "[End System Info]"
                if marker in text:
                    text = text.split(marker, 1)[1].strip()
                if text:
                    yield CoreEvent(
                        kind="user_message_shown", text=text, from_replay=True
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
                    yield CoreEvent(
                        kind="tool_call",
                        tool_name=block.get("name", "tool"),
                        from_replay=True,
                    )
