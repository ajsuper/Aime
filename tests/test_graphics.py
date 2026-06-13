"""Tests for the CreateGraphics validator (aime.graphics.validate).

The validator is the server-side gate that decides whether a graphic gets
rendered or handed back to the model as a retry-able error. It's a cheap check —
the browser is the real renderer — so these lock in the obvious accept/reject
cases per format, especially the SVG safety rejections.
"""

from provider_backend import BackendEvent
from aime import graphics
from aime import graphics_store
from aime import encryption as enc
from aime.controller import ConversationController


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


# --- Controller wiring -----------------------------------------------------
# CreateGraphics is a client tool: the controller validates, saves the graphic to
# the user's GraphicStore (which allocates the `graphic-N` id), stamps the history
# block via register_graphic (for replay), emits a `graphic` CoreEvent for the
# frontend, and hands the model a tool_result naming the id + its [graphic-N] tag.
# GetGraphic reloads a stored source by id from the same store.


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


def _graphics_controller(tmp_path):
    backend = _RecordingBackend()
    events = []
    store = graphics_store.GraphicStore(str(tmp_path / "graphics"),
                                        enc.generate_dek())
    controller = ConversationController(
        backend=backend,
        tool_gateway=object(),  # never touched — CreateGraphics is client-side
        worker_spawner=lambda fn: None,
        graphic_store=store,
    )
    controller.subscribe(events.append)
    return controller, backend, events


def _fire_tool(controller, name, tool_input, tool_use_id="g1"):
    controller._handle_tool_use(BackendEvent(
        kind="tool_use",
        tool_name=name,
        tool_input=tool_input,
        tool_use_id=tool_use_id,
        expects_response=True,
    ))


def test_valid_graphic_stores_and_reports_id(tmp_path):
    controller, backend, events = _graphics_controller(tmp_path)
    spec = '{"mark": "bar", "encoding": {}}'

    _fire_tool(controller, "CreateGraphics", {
        "format": "vega-lite", "source": spec, "summary": "weekly spend",
    })

    # No auto-card: the graphic is a stored asset, displayed only via its tag.
    assert not any(e.kind == "graphic" for e in events)
    # The canonical copy landed in the store, and the history stamp kept the
    # source for replay.
    assert controller._graphic_store.load("graphic-1")["source"] == spec
    assert backend.stamped["graphic-1"]["source"] == spec
    # The model's result names the id, steers it to write the [graphic-N] tag to
    # display it, points at GetGraphic — and never echoes the spec back.
    assert len(backend.responses) == 1
    result = backend.responses[0].tool_result
    assert "graphic-1" in result and "[graphic-1]" in result
    assert "GetGraphic" in result and spec not in result


def test_fenced_vega_source_is_cleaned_before_store(tmp_path):
    controller, backend, events = _graphics_controller(tmp_path)

    _fire_tool(controller, "CreateGraphics", {
        "format": "vega-lite",
        "source": '```json\n{"mark": "bar", "encoding": {}}\n```',
        "summary": "spend",
    })

    # The code fence is stripped, so the store (and the history stamp) get clean JSON.
    assert controller._graphic_store.load("graphic-1")["source"] == \
        '{"mark": "bar", "encoding": {}}'
    assert backend.stamped["graphic-1"]["source"] == '{"mark": "bar", "encoding": {}}'


def test_invalid_graphic_is_handed_back_without_render_or_store(tmp_path):
    controller, backend, events = _graphics_controller(tmp_path)

    _fire_tool(controller, "CreateGraphics", {
        "format": "vega-lite", "source": "{not json", "summary": "x",
    })

    # Nothing rendered, nothing stored — the model must fix and retry.
    assert not any(e.kind == "graphic" for e in events)
    assert controller._graphic_store.list_graphics() == []
    assert len(backend.responses) == 1
    result = backend.responses[0].tool_result
    assert "not rendered" in result.lower()
    assert "CreateGraphics again" in result


def test_get_graphic_returns_stored_source(tmp_path):
    controller, backend, events = _graphics_controller(tmp_path)
    spec = '{"mark": "line", "encoding": {}}'
    _fire_tool(controller, "CreateGraphics",
               {"format": "vega-lite", "source": spec, "summary": "trend"})

    _fire_tool(controller, "GetGraphic", {"id": "graphic-1"}, tool_use_id="g2")

    result = backend.responses[-1].tool_result
    assert spec in result                      # full source handed back to edit
    assert "graphic-1" in result and "CreateGraphics" in result


def test_get_graphic_unknown_id_lists_available(tmp_path):
    controller, backend, events = _graphics_controller(tmp_path)
    _fire_tool(controller, "CreateGraphics",
               {"format": "vega-lite", "source": '{"mark": "bar"}',
                "summary": "s"})

    _fire_tool(controller, "GetGraphic", {"id": "graphic-9"}, tool_use_id="g2")

    result = backend.responses[-1].tool_result
    # not found + what does exist
    assert "graphic-9" in result and "graphic-1" in result
