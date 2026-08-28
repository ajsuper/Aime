"""Gateway wiring for topic freshness/date annotations.

The pure logic lives in aime.freshness and aime.datesidecar and is tested
there. What is pinned here is the plumbing, where the damaging mistakes are:
annotations leaking into stored content, `contents` drifting from what the
model was shown (which silently breaks find/replace), and writes reaching the
backend with no stamp date.
"""

import datetime

import pytest

from aime import freshness
from aime.tool_gateway import ToolGateway
from aime.tool_formatting import format_tool_result_for_model

TODAY = datetime.datetime(2026, 8, 26, 14, 0)

STORED = (
    "## Social\nParty in 3 days on August 22, 2026\n\n"
    "## Health\nWent running\n\n"
    "<!--aime:updated v1\nSocial=2026-08-19\nHealth=2026-08-25\n-->\n"
)


class _Backend:
    def __init__(self, reply):
        self.bodies = []
        self._reply = reply

    def __call__(self, url, json=None, timeout=None):
        self.bodies.append(dict(json or {}))
        reply = self._reply

        class _Resp:
            ok = True

            @staticmethod
            def json():
                return reply
        return _Resp()


@pytest.fixture
def gw(monkeypatch):
    def _make(reply):
        backend = _Backend(reply)
        monkeypatch.setattr("aime.tool_gateway.requests.post", backend)
        gateway = ToolGateway()
        gateway._now_local = lambda: TODAY
        gateway._sent = backend
        return gateway
    return _make


# --- read side ---------------------------------------------------------------

def test_read_strips_the_stored_block_and_attaches_annotations(gw):
    g = gw({"id": 3, "contents": STORED})
    out = g.call("get_topic_contents", id=3)
    assert "aime:updated" not in out["contents"]      # bookkeeping, not content
    assert "<freshness" in out["_annotations"]
    assert "<dates" in out["_annotations"]


def test_annotations_never_enter_contents(gw):
    # contents feeds the web UI and the model's find-strings alike; metadata in
    # there would corrupt both.
    g = gw({"id": 3, "contents": STORED})
    out = g.call("get_topic_contents", id=3)
    assert "<freshness" not in out["contents"]
    assert "<dates" not in out["contents"]


def test_contents_matches_the_stored_body_exactly(gw):
    g = gw({"id": 3, "contents": STORED})
    out = g.call("get_topic_contents", id=3)
    assert out["contents"] == freshness.parse(STORED)[0]


def test_the_reported_staleness_case_is_surfaced(gw):
    g = gw({"id": 3, "contents": STORED})
    ann = g.call("get_topic_contents", id=3)["_annotations"]
    assert "(7 days ago)" in ann          # "in 3 days" was written a week ago
    assert "PAST" in ann                  # August 22 has been and gone


def test_a_read_error_is_passed_through_untouched(gw):
    g = gw({"error": "nope"})
    assert g.call("get_topic_contents", id=3) == {"error": "nope"}


def test_model_render_puts_the_body_first_and_verbatim(gw):
    g = gw({"id": 3, "contents": STORED})
    rendered = format_tool_result_for_model(
        "GetTopicContents", g.call("get_topic_contents", id=3))
    body = freshness.parse(STORED)[0].rstrip()
    assert rendered.startswith(body)


# --- write side --------------------------------------------------------------

def test_replace_is_stamped_with_the_user_local_date(gw):
    g = gw({"ok": True})
    g.call("replace_topic_contents", id=3, contents="## Social\nx\n")
    assert g._sent.bodies[0]["stamp_date"] == "2026-08-26"


def test_edit_is_stamped_too(gw):
    g = gw({"ok": True})
    g.call("edit_topic_contents", id=3,
           patches=[{"find": "a", "replace": "b"}])
    assert g._sent.bodies[0]["stamp_date"] == "2026-08-26"


def test_a_copied_back_annotation_never_reaches_the_backend(gw):
    g = gw({"id": 3, "contents": STORED})
    ann = g.call("get_topic_contents", id=3)["_annotations"]
    g2 = gw({"ok": True})
    g2.call("replace_topic_contents", id=3,
            contents="## Social\nParty\n\n" + ann + "\n")
    sent = g2._sent.bodies[0]["contents"]
    assert "<freshness" not in sent
    assert "<dates" not in sent
    assert "## Social\nParty" in sent


def test_annotations_are_stripped_from_patch_fragments(gw):
    g = gw({"ok": True})
    g.call("edit_topic_contents", id=3, patches=[{
        "find": "## Social\nParty\n\n<dates as-of August 26, 2026>\nx\n</dates>",
        "replace": "## Social\nParty harder",
    }])
    patch = g._sent.bodies[0]["patches"][0]
    assert "<dates" not in patch["find"]
    assert patch["find"].startswith("## Social\nParty")


def test_stripping_a_fragment_does_not_disturb_its_whitespace(gw):
    # A find-string is matched byte-for-byte; normalizing a trailing newline
    # here would silently stop patches applying.
    g = gw({"ok": True})
    g.call("edit_topic_contents", id=3,
           patches=[{"find": "  spaced  \n", "replace": "x"}])
    assert g._sent.bodies[0]["patches"][0]["find"] == "  spaced  \n"


def test_caller_supplied_stamp_date_wins(gw):
    g = gw({"ok": True})
    g.call("replace_topic_contents", id=3, contents="x", stamp_date="2020-01-01")
    assert g._sent.bodies[0]["stamp_date"] == "2020-01-01"


def test_other_tools_are_not_stamped(gw):
    g = gw({"ok": True})
    g.call("create_topic", title="x", summary="y", category="z")
    assert "stamp_date" not in g._sent.bodies[0]


# --- event references --------------------------------------------------------

def test_event_refs_resolve_against_the_live_calendar(monkeypatch):
    calls = []

    def fake_post(url, json=None, timeout=None):
        calls.append(dict(json or {}))
        name = (json or {}).get("tool_name")

        class _Resp:
            ok = True

            @staticmethod
            def json():
                if name == "get_topic_contents":
                    return {"id": 3, "contents": "## Social\nParty is [event-42]\n"}
                return [{"id": 42, "title": "Rachel's party",
                         "date": "05/09/2026", "status": "scheduled"}]
        return _Resp()

    monkeypatch.setattr("aime.tool_gateway.requests.post", fake_post)
    g = ToolGateway()
    g._now_local = lambda: TODAY
    ann = g.call("get_topic_contents", id=3)["_annotations"]
    assert "<events" in ann
    assert "September 5, 2026" in ann       # live date, not whatever prose said
    assert any(c.get("tool_name") == "get_events" for c in calls)


def test_no_event_lookup_when_a_topic_has_no_refs(gw):
    # The common case must cost nothing extra.
    g = gw({"id": 3, "contents": STORED})
    g.call("get_topic_contents", id=3)
    assert [b["tool_name"] for b in g._sent.bodies] == ["get_topic_contents"]


def test_event_lookup_failure_does_not_break_the_read(monkeypatch):
    def fake_post(url, json=None, timeout=None):
        name = (json or {}).get("tool_name")

        class _Resp:
            ok = True

            @staticmethod
            def json():
                if name == "get_topic_contents":
                    return {"id": 3, "contents": "Party is [event-42]\n"}
                return {"error": "calendar unavailable"}
        return _Resp()

    monkeypatch.setattr("aime.tool_gateway.requests.post", fake_post)
    g = ToolGateway()
    g._now_local = lambda: TODAY
    out = g.call("get_topic_contents", id=3)
    assert out["contents"] == "Party is [event-42]\n"
    assert "<events" not in out.get("_annotations", "")


def test_event_block_is_stripped_on_write(gw):
    g = gw({"ok": True})
    g.call("replace_topic_contents", id=3, contents=(
        "Party is [event-42]\n\n<events as-of August 26, 2026>\nx\n</events>\n"))
    sent = g._sent.bodies[0]["contents"]
    assert "<events" not in sent
    assert "[event-42]" in sent      # the tag is content and must survive
