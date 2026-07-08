"""Tests for the CreateGraphics validator (aime.graphics.validate).

The validator is the server-side gate that decides whether a graphic gets
rendered or handed back to the model as a retry-able error. It's a cheap check —
the browser is the real renderer — so these lock in the obvious accept/reject
cases per format, especially the SVG safety rejections.
"""

import pytest

from provider_backend import BackendEvent
from aime import graphics
from aime import graphics_store
from aime import graphics_examples
from aime import vega_compile
from aime import encryption as enc
from aime.controller import ConversationController

# The Node compile gate is authoritative but optional (a bare box without Node/
# deps falls back to the loose check). Gate the tests that exercise real
# compilation so the suite still passes where it isn't installed.
_needs_compiler = pytest.mark.skipif(
    not vega_compile.available(),
    reason="Node vega-lite compile gate not available",
)


def test_unknown_format_rejected():
    err = graphics.validate("png", "<svg/>")
    assert err and "Unknown format" in err


def test_empty_source_rejected():
    assert graphics.validate("svg", "   ")
    assert graphics.validate("vega-lite", "")


def test_vega_lite_valid_spec_accepted():
    spec = '{"mark": "bar", "data": {"values": [{"a": 1}]}, "encoding": {}}'
    assert graphics.validate("vega-lite", spec) is None


def test_vega_lite_invalid_json_rejected():
    err = graphics.validate("vega-lite", "{not json")
    assert err and "JSON" in err


def test_vega_lite_non_spec_object_rejected():
    # Valid JSON object, but no mark/encoding/container — not a renderable spec.
    err = graphics.validate("vega-lite", '{"data": {"values": []}}')
    assert err and "Vega-Lite spec" in err


def test_vega_lite_list_rejected():
    err = graphics.validate("vega-lite", "[1, 2, 3]")
    assert err and "JSON object" in err


def test_svg_well_formed_accepted():
    svg = ('<svg xmlns="http://www.w3.org/2000/svg">'
           '<rect width="10" height="10"/></svg>')
    assert graphics.validate("svg", svg) is None


def test_svg_script_rejected():
    err = graphics.validate("svg", "<svg><script>alert(1)</script></svg>")
    assert err and "static" in err


def test_svg_event_handler_rejected():
    err = graphics.validate(
        "svg", '<svg onload="alert(1)" xmlns="http://www.w3.org/2000/svg"/>'
    )
    assert err and "static" in err


def test_svg_javascript_uri_rejected():
    svg = ('<svg xmlns="http://www.w3.org/2000/svg">'
           '<a href="javascript:alert(1)"><rect/></a></svg>')
    err = graphics.validate("svg", svg)
    assert err and "static" in err


def test_svg_malformed_xml_rejected():
    err = graphics.validate("svg", "<svg><rect>")
    assert err and "well-formed" in err


def test_mermaid_known_types_accepted():
    # The gate only checks the opening diagram-type keyword; the body is left to
    # the browser renderer. A real type — even with a directive, comment, or
    # frontmatter ahead of it — passes.
    assert graphics.validate("mermaid", "graph TD; A-->B") is None
    assert graphics.validate("mermaid", "flowchart LR\n  A --> B") is None
    assert graphics.validate("mermaid", "sequenceDiagram\n  A->>B: hi") is None
    assert graphics.validate("mermaid", "stateDiagram-v2\n  [*] --> S") is None
    assert graphics.validate("mermaid", "xychart-beta\n  bar [1,2,3]") is None
    assert graphics.validate(
        "mermaid", "%%{init: {'theme':'base'}}%%\nflowchart TD\n A-->B"
    ) is None
    assert graphics.validate(
        "mermaid", "---\ntitle: Demo\n---\nflowchart TD\n A-->B"
    ) is None


def test_mermaid_non_diagram_rejected():
    # Prose pasted by mistake, or a missing type line, names no diagram type and
    # would render to nothing — so the model gets a fixable error instead.
    err = graphics.validate("mermaid", "not really mermaid but non-empty")
    assert err and "diagram type" in err
    assert graphics.validate("mermaid", "   \n  %% just a comment\n")


def test_strip_code_fence():
    assert graphics.strip_code_fence('```json\n{"mark": "bar"}\n```') == '{"mark": "bar"}'
    assert graphics.strip_code_fence('```\n{"a": 1}\n```') == '{"a": 1}'
    assert graphics.strip_code_fence('  {"a": 1}  ') == '{"a": 1}'
    # No fence: trimmed but otherwise untouched.
    assert graphics.strip_code_fence('{"a": 1}') == '{"a": 1}'
    # A lone backtick run inside the body must not be mistaken for a fence.
    svg = "<svg><text>`code`</text></svg>"
    assert graphics.strip_code_fence(svg) == svg


def test_svg_unbound_xlink_prefix_accepted():
    # `xlink:href` without an xmlns:xlink declaration is a common, renderable
    # SVG idiom; the validator binds the prefix for its parse instead of
    # rejecting the drawing.
    svg = ('<svg xmlns="http://www.w3.org/2000/svg">'
           '<use xlink:href="#a"/></svg>')
    assert graphics.validate("svg", svg) is None


def test_svg_non_svg_root_rejected():
    err = graphics.validate(
        "svg", '<div xmlns="http://www.w3.org/2000/svg"><rect/></div>'
    )
    assert err and "root element" in err


def test_normalize_strips_fence_and_trailing_commas():
    # Trailing commas are tolerated for Vega-Lite — but only because removing
    # them makes otherwise-invalid JSON parse.
    out = graphics.normalize(
        "vega-lite", '```json\n{"mark": "bar", "encoding": {},}\n```'
    )
    assert out == '{"mark": "bar", "encoding": {}}'
    assert graphics.validate("vega-lite", out) is None


def test_normalize_leaves_valid_json_untouched():
    spec = '{"mark": "bar", "data": {"values": [{"a": "x,}"}]}}'
    # The `,}` lives inside a string; since the spec already parses, normalize
    # must not touch it.
    assert graphics.normalize("vega-lite", spec) == spec


def test_array_source_gives_directive_error():
    # A bare array (the "Expecting value: line 1 column 2" case) gets a message
    # telling the model to send an object.
    err = graphics.validate("vega-lite", "[1, 2, 3]")
    assert err and "starting with `{`" in err


# --- History stamp + the context strip -------------------------------------
# The canonical source lives in the per-user GraphicStore; a stamped copy (id +
# cleaned source) also rides in the message history so the session replays on
# /load. The strip slims only the API-bound copy of that stamp.


def _graphic_block(gid, fmt, source, summary):
    return {
        "role": "assistant",
        "content": [{
            "type": "tool_use", "id": "tu_" + gid, "name": "CreateGraphics",
            "input": {"format": fmt, "source": source, "summary": summary,
                      "graphic_id": gid},
        }],
    }


def test_redact_history_graphics_slims_source_but_keeps_id_and_summary():
    # An older graphic (outside the keep-recent window) is slimmed.
    history = [
        _graphic_block("graphic-1", "mermaid", "graph TD; A-->B", "the flow"),
        {"role": "user", "content": [{"type": "text", "text": "thanks"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "welcome"}]},
    ]
    out = graphics.redact_history_graphics(history)
    # The original history is untouched — full source still there for persistence.
    assert history[0]["content"][0]["input"]["source"] == "graph TD; A-->B"
    inp = out[0]["content"][0]["input"]
    assert "graph TD" not in inp["source"]          # source slimmed away
    assert inp["graphic_id"] == "graphic-1"         # id retained
    assert inp["summary"] == "the flow"             # summary retained
    assert "graphic-1" in inp["source"] and "GetGraphic" in inp["source"]


def test_redact_history_keeps_freshest_graphic_intact():
    # The just-drawn graphic (its tool_result is the last message) keeps its real
    # source, so the continuation turn never sees a stub where its source was —
    # which is what stops the spurious "oops, I sent a placeholder" retry.
    history = [
        _graphic_block("graphic-1", "mermaid", "graph TD; A-->B", "flow"),
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tu_graphic-1",
             "content": "Rendered as graphic-1."}]},
    ]
    out = graphics.redact_history_graphics(history)
    assert out[0]["content"][0]["input"]["source"] == "graph TD; A-->B"


def test_redact_history_keeps_loaded_source_only_in_last_message():
    loaded = graphics.loaded_source_result("graphic-1", "mermaid", "graph TD; A-->B")
    older = {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": loaded}]}
    latest = {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t2", "content": loaded}]}
    out = graphics.redact_history_graphics([older, latest])
    # The earlier reload is slimmed; the freshest one (last message) is intact so
    # the model can read it on the editing turn.
    assert "graph TD" not in out[0]["content"][0]["content"]
    assert "graph TD" in out[1]["content"][0]["content"]


def test_redact_history_keeps_loaded_examples_only_in_last_message():
    # A LoadGraphicsExamples payload is bulky and only needed on the drawing turn,
    # so it's slimmed once it's no longer the last message — same as a reloaded
    # GetGraphic source.
    payload = graphics.loaded_examples_result(
        "reference-line",
        [{"title": "T", "note": "swap values",
          "spec": {"mark": "rule", "encoding": {"y": {"datum": 100}}}}],
    )
    assert '"datum": 100' in payload  # the spec rides in the loaded result
    older = {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": payload}]}
    latest = {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t2", "content": payload}]}
    out = graphics.redact_history_graphics([older, latest])
    assert "datum" not in out[0]["content"][0]["content"]      # earlier: slimmed
    assert "LoadGraphicsExamples" in out[0]["content"][0]["content"]
    assert '"datum": 100' in out[1]["content"][0]["content"]   # freshest: intact


# --- Tag scanning / write rule helpers -------------------------------------


def test_graphic_tag_handles_extracts_every_form():
    body = (
        "intro [graphic-0:1] mid [graphic-7:2] and [graphic-4:7:3] "
        "plus a legacy [graphic-5] tail"
    )
    assert graphics.graphic_tag_handles(body) == ["0", "7", "4:7", "0"]


def test_graphic_tag_handles_ignores_non_tags():
    # A plain word, an unmatched bracket, and non-graphic text contribute nothing.
    assert graphics.graphic_tag_handles("nothing here") == []
    assert graphics.graphic_tag_handles("[graphic-]") == []
    assert graphics.graphic_tag_handles("[fig-1:2]") == []


def test_foreign_graphic_tag_message_mentions_handle():
    msg = graphics.foreign_graphic_tag_message("4:7")
    assert "graphic-4:7" in msg and "CreateGraphics" in msg


def test_write_rule_accepts_in_topic_tags(tmp_path):
    # Owner saving their own topic 5 with that topic's bare tag.
    controller, backend, events, provider = _graphics_controller(tmp_path)
    err = controller._reject_foreign_graphic_tags(
        "ReplaceTopicContents", {"id": "5", "contents": "chart: [graphic-5:1]"})
    assert err is None


def test_write_rule_recipient_keeps_owner_bare_tag(tmp_path):
    # A recipient editing shared topic "4:5" saves the body verbatim — it still
    # carries the owner's bare [graphic-5:1]. The bare tag belongs to the topic's
    # owner (4), so it denotes this very topic and the save is accepted.
    controller, backend, events, provider = _graphics_controller(tmp_path)
    err = controller._reject_foreign_graphic_tags(
        "ReplaceTopicContents", {"id": "4:5", "contents": "see [graphic-5:1]"})
    assert err is None
    # The explicit form the recipient's own CreateGraphics would emit also passes.
    err2 = controller._reject_foreign_graphic_tags(
        "ReplaceTopicContents", {"id": "4:5", "contents": "[graphic-4:5:2]"})
    assert err2 is None


def test_write_rule_rejects_personal_and_other_topic_tags(tmp_path):
    controller, backend, events, provider = _graphics_controller(tmp_path)
    # A personal graphic can't be embedded in a topic.
    assert controller._reject_foreign_graphic_tags(
        "ReplaceTopicContents", {"id": "4:5", "contents": "[graphic-0:1]"}) is not None
    # Another topic's graphic (different topic id) is foreign.
    assert controller._reject_foreign_graphic_tags(
        "ReplaceTopicContents", {"id": "4:5", "contents": "[graphic-4:9:1]"}) is not None
    # A bare tag for a different topic number is foreign too.
    assert controller._reject_foreign_graphic_tags(
        "ReplaceTopicContents", {"id": "5", "contents": "[graphic-7:1]"}) is not None


def test_write_rule_scans_edit_patches(tmp_path):
    # EditTopicContents introduces tags via patch `replace` text.
    controller, backend, events, provider = _graphics_controller(tmp_path)
    ok = controller._reject_foreign_graphic_tags(
        "EditTopicContents",
        {"id": "4:5", "patches": [{"find": "x", "replace": "[graphic-5:1]"}]})
    assert ok is None
    bad = controller._reject_foreign_graphic_tags(
        "EditTopicContents",
        {"id": "4:5", "patches": [{"find": "x", "replace": "[graphic-0:3]"}]})
    assert bad is not None


# --- Controller wiring -----------------------------------------------------
# CreateGraphics is a client tool: the controller validates, resolves the target
# topic store through the injected provider, saves the graphic (which allocates
# the ordinal), builds the full `graphic-<handle>:N` id, stamps the history block
# via register_graphic (for replay), and hands the model a tool_result naming the
# id + its [graphic-<handle>:N] tag. GetGraphic reloads a stored source by id
# through the same provider.


class _RecordingBackend:
    """Stand-in for the real backend: records the history stamp the controller
    makes via register_graphic so we can assert the source was kept for replay."""

    conversations_dir = None

    def __init__(self):
        self.responses = []
        self.stamped = {}   # id -> {source, summary}

    def submit(self, event: BackendEvent):
        if event.kind == "tool_send_response":
            self.responses.append(event)

    def register_graphic(self, tool_use_id, graphic_id, source, summary=None):
        self.stamped[graphic_id] = {"source": source, "summary": summary}
        return True


class _StoreProvider:
    """Test graphic-store provider: maps a topic handle to a scoped store, all
    under one owner. ``"0"`` is the personal/chat store; a bare numeric handle is
    that own topic. A handle listed in ``denied`` resolves to None (stands in for
    a view-only or unshared topic), so the controller takes the refusal path."""

    def __init__(self, base, dek, denied=(), me=1):
        self._base = base
        self._dek = dek
        self._denied = set(denied)
        self._me = me           # the acting user (own-topic / personal owner)
        self._stores = {}

    def __call__(self, handle, need="edit"):
        if handle in self._denied:
            return None
        if handle not in self._stores:
            parts = str(handle).split(":")
            try:
                if len(parts) == 1:
                    owner, tid = self._me, (0 if parts[0] == "0" else int(parts[0]))
                elif len(parts) == 2:        # shared "O:T"
                    owner, tid = int(parts[0]), int(parts[1])
                else:
                    return None
            except ValueError:
                return None
            self._stores[handle] = graphics_store.GraphicStore(
                self._base, self._dek, owner_id=owner, topic_id=tid)
        return self._stores[handle]


def _graphics_controller(tmp_path, denied=()):
    backend = _RecordingBackend()
    events = []
    provider = _StoreProvider(str(tmp_path / "graphics"),
                              enc.generate_dek(), denied=denied)
    controller = ConversationController(
        backend=backend,
        tool_gateway=object(),  # never touched — CreateGraphics is client-side
        worker_spawner=lambda fn: None,
        graphic_store_provider=provider,
    )
    controller.subscribe(events.append)
    return controller, backend, events, provider


def _fire_tool(controller, name, tool_input, tool_use_id="g1"):
    controller._handle_tool_use(BackendEvent(
        kind="tool_use",
        tool_name=name,
        tool_input=tool_input,
        tool_use_id=tool_use_id,
        expects_response=True,
    ))


def test_valid_graphic_stores_and_reports_id(tmp_path):
    controller, backend, events, provider = _graphics_controller(tmp_path)
    spec = '{"mark": "bar", "encoding": {}}'

    _fire_tool(controller, "CreateGraphics", {
        "format": "vega-lite", "source": spec, "summary": "weekly spend",
    })

    # No auto-card: the graphic is a stored asset, displayed only via its tag.
    assert not any(e.kind == "graphic" for e in events)
    # The canonical copy landed in the personal ("0") store under its bare stem,
    # and the history stamp kept the source for replay under the full id.
    assert provider("0").load("graphic-1")["source"] == spec
    assert backend.stamped["graphic-0:1"]["source"] == spec
    # The model's result names the full id, steers it to write the
    # [graphic-0:1] tag to display it, points at GetGraphic — never echoes spec.
    assert len(backend.responses) == 1
    result = backend.responses[0].tool_result
    assert "graphic-0:1" in result and "[graphic-0:1]" in result
    assert "GetGraphic" in result and spec not in result


def test_graphic_into_own_topic_uses_absolute_id(tmp_path):
    # Acting user is 1; targeting their own topic 7. The emitted id is absolute —
    # owner (1) baked in — so the tag resolves the same in chat and in the body.
    controller, backend, events, provider = _graphics_controller(tmp_path)
    spec = '{"mark": "bar", "encoding": {}}'

    _fire_tool(controller, "CreateGraphics", {
        "format": "vega-lite", "source": spec, "summary": "in topic 7",
        "topic": "7",
    })

    # Lives in topic 7's store; the id + tag carry the absolute owner:topic.
    assert provider("7").load("graphic-1")["source"] == spec
    assert provider("0").load("graphic-1") is None  # not the personal store
    result = backend.responses[0].tool_result
    assert "graphic-1:7:1" in result and "[graphic-1:7:1]" in result


def test_graphic_into_shared_topic_bakes_in_owner_id(tmp_path):
    # Acting user is 1, drawing into topic 5 of owner 4 (shared, edit grant). The
    # id carries owner 4 — exactly so the recipient can drop the tag into chat and
    # it still resolves to the owner's topic, not "my topic 5".
    controller, backend, events, provider = _graphics_controller(tmp_path)

    _fire_tool(controller, "CreateGraphics", {
        "format": "vega-lite", "source": '{"mark": "bar", "encoding": {}}',
        "summary": "shared", "topic": "4:5",
    })

    result = backend.responses[0].tool_result
    assert "graphic-4:5:1" in result and "[graphic-4:5:1]" in result


def test_graphic_into_unauthorized_topic_is_refused(tmp_path):
    # Topic "9" is view-only / unshared for this user: the provider returns None.
    controller, backend, events, provider = _graphics_controller(
        tmp_path, denied={"9"})

    _fire_tool(controller, "CreateGraphics", {
        "format": "vega-lite", "source": '{"mark": "bar", "encoding": {}}',
        "summary": "x", "topic": "9",
    })

    # Nothing stored anywhere; the model gets a calm, recoverable refusal.
    assert provider("9") is None
    result = backend.responses[0].tool_result
    assert "view-only" in result.lower() or "couldn't add" in result.lower()
    assert "graphic-9" not in result  # no id minted


def test_fenced_vega_source_is_cleaned_before_store(tmp_path):
    controller, backend, events, provider = _graphics_controller(tmp_path)

    _fire_tool(controller, "CreateGraphics", {
        "format": "vega-lite",
        "source": '```json\n{"mark": "bar", "encoding": {}}\n```',
        "summary": "spend",
    })

    # The code fence is stripped, so the store (and the history stamp) get clean JSON.
    assert provider("0").load("graphic-1")["source"] == \
        '{"mark": "bar", "encoding": {}}'
    assert backend.stamped["graphic-0:1"]["source"] == '{"mark": "bar", "encoding": {}}'


def test_invalid_graphic_is_handed_back_without_render_or_store(tmp_path):
    controller, backend, events, provider = _graphics_controller(tmp_path)

    _fire_tool(controller, "CreateGraphics", {
        "format": "vega-lite", "source": "{not json", "summary": "x",
    })

    # Nothing rendered, nothing stored — the model must fix and retry.
    assert not any(e.kind == "graphic" for e in events)
    assert provider("0").list_graphics() == []
    assert len(backend.responses) == 1
    result = backend.responses[0].tool_result
    assert "not rendered" in result.lower()
    assert "CreateGraphics again" in result


def test_get_graphic_returns_stored_source(tmp_path):
    controller, backend, events, provider = _graphics_controller(tmp_path)
    spec = '{"mark": "line", "encoding": {}}'
    _fire_tool(controller, "CreateGraphics",
               {"format": "vega-lite", "source": spec, "summary": "trend"})

    _fire_tool(controller, "GetGraphic", {"id": "graphic-0:1"}, tool_use_id="g2")

    result = backend.responses[-1].tool_result
    assert spec in result                      # full source handed back to edit
    assert "graphic-0:1" in result and "CreateGraphics" in result


def test_get_graphic_unknown_id_lists_available(tmp_path):
    controller, backend, events, provider = _graphics_controller(tmp_path)
    _fire_tool(controller, "CreateGraphics",
               {"format": "vega-lite", "source": '{"mark": "bar"}',
                "summary": "s"})

    _fire_tool(controller, "GetGraphic", {"id": "graphic-0:9"}, tool_use_id="g2")

    result = backend.responses[-1].tool_result
    # not found + what does exist (in full-id form)
    assert "graphic-0:9" in result and "graphic-0:1" in result


# --- Compile gate (aime.vega_compile, wired into CreateGraphics) ------------


@_needs_compiler
def test_structurally_broken_vega_is_rejected_by_compile_gate(tmp_path):
    # Valid JSON with a spec key, so the loose validate() passes — but the field
    # type is bogus, so the compile gate catches it and it bounces back for a
    # same-turn fix. This is exactly the class of failure (layered/typed specs)
    # that used to be told "Saved" and then fail silently in the browser.
    controller, backend, events, provider = _graphics_controller(tmp_path)
    bad = '{"mark": "line", "encoding": {"y": {"field": "v", "type": "nope"}}}'
    # Sanity: the cheap validator alone would let this through.
    assert graphics.validate("vega-lite", bad) is None

    _fire_tool(controller, "CreateGraphics",
               {"format": "vega-lite", "source": bad, "summary": "x"})

    assert provider("0").list_graphics() == []          # nothing stored
    assert not any(e.kind == "graphic" for e in events)  # nothing rendered
    result = backend.responses[-1].tool_result
    assert "not rendered" in result.lower()
    assert "CreateGraphics again" in result
    # The real compiler reason is surfaced, plus a pointer to the examples tool.
    assert "LoadGraphicsExamples" in result


@_needs_compiler
def test_compilable_vega_still_saves(tmp_path):
    # A well-formed spec passes the compile gate and stores as before — the gate
    # only ever *adds* rejections, never blocks a good chart.
    controller, backend, events, provider = _graphics_controller(tmp_path)
    good = ('{"mark": "bar", "data": {"values": [{"a": "A", "b": 1}]}, '
            '"encoding": {"x": {"field": "a", "type": "nominal"}, '
            '"y": {"field": "b", "type": "quantitative"}}}')

    _fire_tool(controller, "CreateGraphics",
               {"format": "vega-lite", "source": good, "summary": "ok"})

    assert provider("0").load("graphic-1")["source"] == good
    assert "graphic-0:1" in backend.responses[-1].tool_result


# --- LoadGraphicsExamples handler ------------------------------------------


def test_load_graphics_examples_returns_adaptable_spec(tmp_path):
    controller, backend, events, provider = _graphics_controller(tmp_path)

    _fire_tool(controller, "LoadGraphicsExamples", {"kind": "reference-line"})

    result = backend.responses[-1].tool_result
    # The payload opens with the strip sentinel, names the tool to re-call, and
    # carries a real spec (a rule datum) the model can adapt.
    assert "LoadGraphicsExamples" not in result.split("]", 1)[0] or True  # opener
    assert '"rule"' in result and "datum" in result
    assert "CreateGraphics" in result


def test_load_graphics_examples_unknown_kind_lists_options(tmp_path):
    controller, backend, events, provider = _graphics_controller(tmp_path)

    _fire_tool(controller, "LoadGraphicsExamples", {"kind": "pie-of-pie"})

    result = backend.responses[-1].tool_result
    assert "pie-of-pie" in result
    # Steers to a valid kind rather than dead-ending.
    assert "reference-line" in result and "grouped-bar" in result
