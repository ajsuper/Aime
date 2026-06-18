"""Tests for per-turn model routing (``aime.model_router``).

Routing decides whether a turn runs on the cheap model or the expensive one,
so the assertions here are really about **cost and safety**: the router must
downgrade to Haiku *only* on read-only turns it actually classified as easy,
and must fall back to Sonnet on every uncertain or failing path (disabled,
mid-tool-loop continuation, images, no user text, or a classifier error) — it
"must never block or break a turn".

No network: the Anthropic client is replaced with a fake before the router is
constructed.
"""

import pytest

from aime import model_router as mr_mod
from aime.model_router import ModelRouter


HAIKU = "haiku-id"
SONNET = "sonnet-id"
ROUTER = "router-id"


# --------------------------------------------------------------------------- #
# Fake Anthropic client
# --------------------------------------------------------------------------- #
class _Block:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _Resp:
    def __init__(self, text):
        self.content = [_Block(text)]
        self.usage = {"input_tokens": 10, "output_tokens": 1}


class _FakeMessages:
    def __init__(self, parent):
        self._parent = parent

    def create(self, **kwargs):
        self._parent.calls.append(kwargs)
        if self._parent.raises is not None:
            raise self._parent.raises
        return _Resp(self._parent.decision_text)


class _FakeClient:
    def __init__(self, decision_text="EASY", raises=None):
        self.decision_text = decision_text
        self.raises = raises
        self.calls = []
        self.messages = _FakeMessages(self)


@pytest.fixture
def make_router(monkeypatch):
    """Build a ModelRouter whose Anthropic client is a fake. Returns
    (router, fake_client) so a test can both drive and inspect it."""
    def _make(decision_text="EASY", raises=None, enabled=True, record_api=None):
        fake = _FakeClient(decision_text=decision_text, raises=raises)
        monkeypatch.setattr(mr_mod, "Anthropic", lambda **kw: fake)
        router = ModelRouter(
            haiku_model=HAIKU, sonnet_model=SONNET, router_model=ROUTER,
            enabled=enabled, usage_label="u", record_api=record_api,
        )
        return router, fake
    return _make


def _user(text):
    return {"role": "user", "content": text}


# --------------------------------------------------------------------------- #
# Gating: paths that skip classification and pin Sonnet
# --------------------------------------------------------------------------- #
def test_disabled_router_always_sonnet_without_calling_model(make_router):
    router, fake = make_router(enabled=False)
    assert router.choose([_user("what's on friday?")]) == (SONNET, "sonnet")
    assert fake.calls == []  # never spends a classifier call when disabled


def test_continuation_turn_stays_sonnet(make_router):
    router, fake = make_router(decision_text="EASY")
    # Even though the classifier *would* say EASY, a mid-tool-loop continuation
    # must not downgrade — it would strand tool_use blocks.
    assert router.choose([_user("hi")], is_continuation=True) == (SONNET, "sonnet")
    assert fake.calls == []


def test_image_turn_stays_sonnet(make_router):
    router, fake = make_router(decision_text="EASY")
    assert router.choose([_user("look at this")], has_images=True) == (SONNET, "sonnet")
    assert fake.calls == []


def test_no_user_text_is_sonnet(make_router):
    router, fake = make_router(decision_text="EASY")
    assert router.choose([{"role": "assistant", "content": "hello"}]) == (SONNET, "sonnet")
    assert fake.calls == []


# --------------------------------------------------------------------------- #
# Classification outcomes
# --------------------------------------------------------------------------- #
def test_easy_routes_to_haiku(make_router):
    router, _ = make_router(decision_text="EASY")
    assert router.choose([_user("what do I have thursday?")]) == (HAIKU, "haiku")


def test_hard_routes_to_sonnet(make_router):
    router, _ = make_router(decision_text="HARD")
    assert router.choose([_user("plan my week")]) == (SONNET, "sonnet")


def test_easy_with_trailing_punctuation_still_parses(make_router):
    # max_tokens=4 occasionally yields "EASY." — parsing is deliberately lenient.
    router, _ = make_router(decision_text="EASY.\n")
    assert router.choose([_user("what folders do I have?")]) == (HAIKU, "haiku")


def test_unrecognized_classifier_output_defaults_to_sonnet(make_router):
    router, _ = make_router(decision_text="maybe?")
    assert router.choose([_user("???")]) == (SONNET, "sonnet")


def test_classifier_exception_falls_back_to_sonnet(make_router):
    router, _ = make_router(raises=RuntimeError("timeout"))
    # A failing classifier must never break the turn — it falls through.
    assert router.choose([_user("what's on friday?")]) == (SONNET, "sonnet")


# --------------------------------------------------------------------------- #
# Usage accounting hook
# --------------------------------------------------------------------------- #
def test_record_api_called_with_route_purpose(make_router):
    seen = []
    router, _ = make_router(decision_text="EASY",
                            record_api=lambda *a, **k: seen.append((a, k)))
    router.choose([_user("what's on friday?")], session_id="sess-1")
    assert len(seen) == 1
    (label, model, _usage), kwargs = seen[0]
    assert label == "u" and model == ROUTER
    assert kwargs["purpose"] == "route"
    assert kwargs["session_id"] == "sess-1"


def test_record_api_failure_does_not_break_routing(make_router):
    def _boom(*a, **k):
        raise ValueError("ledger down")
    router, _ = make_router(decision_text="EASY", record_api=_boom)
    # The decision still stands even if accounting raises.
    assert router.choose([_user("what's on friday?")]) == (HAIKU, "haiku")


# --------------------------------------------------------------------------- #
# _last_user_text — what the classifier actually sees
# --------------------------------------------------------------------------- #
def test_last_user_text_plain_string():
    assert ModelRouter._last_user_text([_user("  hello  ")]) == "hello"


def test_last_user_text_joins_text_blocks():
    msgs = [{"role": "user", "content": [
        {"type": "text", "text": "one"},
        {"type": "text", "text": "two"},
    ]}]
    assert ModelRouter._last_user_text(msgs) == "one\n\ntwo"


def test_last_user_text_picks_most_recent_user_turn():
    msgs = [
        _user("old"),
        {"role": "assistant", "content": "reply"},
        _user("new"),
    ]
    assert ModelRouter._last_user_text(msgs) == "new"


def test_last_user_text_tool_result_only_is_empty():
    # A user message that is only tool_results means we're mid-loop; classifying
    # it would be wrong, so it resolves to empty (=> caller routes Sonnet).
    msgs = [{"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": "data"},
    ]}]
    assert ModelRouter._last_user_text(msgs) == ""


def test_last_user_text_no_user_messages_is_empty():
    assert ModelRouter._last_user_text([{"role": "assistant", "content": "x"}]) == ""


# --------------------------------------------------------------------------- #
# Misc surface
# --------------------------------------------------------------------------- #
def test_labels_returns_both_poles(make_router):
    router, _ = make_router()
    assert router.labels() == (HAIKU, SONNET)


def test_classifier_prompt_is_capped(make_router):
    router, fake = make_router(decision_text="EASY")
    router.choose([_user("x" * 5000)])
    sent = fake.calls[0]["messages"][0]["content"]
    assert len(sent) <= 1500  # long pastes don't run up the classifier bill
