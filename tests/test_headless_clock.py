"""A background-agent run must see the clock on *every* turn, not just its first.

The per-turn `<clock>` block is appended to the last message only when that
message is a fresh user-text turn — on a tool_result turn it would become the
only "textual" thing in the turn and derail a chat's narration. A headless run
has exactly one user-text turn in its whole life (the kickoff), so that rule
left the worker date-blind from its second turn onward: every event read, every
comparison against "now", and the final summary ran with no date in context, and
the model fell back on training-data priors — reporting the wrong year, and
treating an event a fortnight out as happening today.

There is no one to narrate to in a headless run, so the clock rides on every
turn there. These tests pin both halves: headless keeps it, chat still doesn't.
"""

import pytest

import aime.encryption as _enc
from provider_backend import AnthropicMessagesBackend


def _backend(tmp_path, monkeypatch, *, headless):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    b = AnthropicMessagesBackend(
        system_prompt="sys", model="model", schema_files=[],
        conversations_dir=str(tmp_path), dek=_enc.generate_dek(),
        headless=headless,
    )
    b.new_session()
    return b


def _clocks(messages):
    """Every <clock> block in the last message of an API-bound copy."""
    content = messages[-1].get("content") or []
    return [
        b for b in content
        if isinstance(b, dict)
        and isinstance(b.get("text"), str)
        and "<clock" in b["text"]
    ]


def _kickoff():
    return {"role": "user", "content": [{"type": "text", "text": "[system: do the task]"}]}


def _tool_result_turn():
    return {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "[]"}],
    }


def test_headless_run_still_has_the_clock_after_its_first_turn(tmp_path, monkeypatch):
    b = _backend(tmp_path, monkeypatch, headless=True)
    history = [
        _kickoff(),
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1",
                                           "name": "GetEvents", "input": {}}]},
        _tool_result_turn(),
    ]
    assert len(_clocks(b._cacheable_messages(history))) == 1


def test_chat_still_withholds_the_clock_on_tool_result_turns(tmp_path, monkeypatch):
    b = _backend(tmp_path, monkeypatch, headless=False)
    history = [
        {"role": "user", "content": [{"type": "text", "text": "what's on today?"}]},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1",
                                           "name": "GetEvents", "input": {}}]},
        _tool_result_turn(),
    ]
    assert _clocks(b._cacheable_messages(history)) == []


@pytest.mark.parametrize("headless", [True, False])
def test_a_user_text_turn_always_carries_exactly_one_clock(tmp_path, monkeypatch, headless):
    b = _backend(tmp_path, monkeypatch, headless=headless)
    assert len(_clocks(b._cacheable_messages([_kickoff()]))) == 1


def test_the_clock_stays_outside_the_cached_prefix(tmp_path, monkeypatch):
    """The breakpoint must sit on the block *before* the volatile clock, or
    every turn rewrites the whole cached history."""
    b = _backend(tmp_path, monkeypatch, headless=True)
    content = b._cacheable_messages([_kickoff()])[-1]["content"]
    assert "<clock" in content[-1]["text"]
    assert "cache_control" not in content[-1]
    assert content[-2]["cache_control"]["type"] == "ephemeral"


def test_history_is_never_mutated_by_the_clock(tmp_path, monkeypatch):
    """The clock lives only on the API-bound copy — a persisted/replayed
    history that accumulated stale clocks would be worse than none."""
    b = _backend(tmp_path, monkeypatch, headless=True)
    history = [_kickoff()]
    b._cacheable_messages(history)
    assert history[0]["content"] == [{"type": "text", "text": "[system: do the task]"}]
