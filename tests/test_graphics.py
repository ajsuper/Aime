"""Tests for the CreateGraphics validator (aime.graphics.validate).

The validator is the server-side gate that decides whether a graphic gets
rendered or handed back to the model as a retry-able error. It's a cheap check —
the browser is the real renderer — so these lock in the obvious accept/reject
cases per format, especially the SVG safety rejections.
"""

from provider_backend import BackendEvent
from aime import graphics
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


# --- History store: ids, lookup, and the context strip ---------------------
# The full source lives in the message history (so graphics survive reload and
# can be reloaded for editing); the strip slims only the API-bound copy.


def _graphic_block(gid, fmt, source, summary):
    return {
        "role": "assistant",
        "content": [{
            "type": "tool_use", "id": "tu_" + gid, "name": "CreateGraphics",
            "input": {"format": fmt, "source": source, "summary": summary,
                      "graphic_id": gid},
        }],
    }


def test_next_graphic_id_counts_from_history():
    assert graphics.next_graphic_id([]) == "fig-1"
    history = [_graphic_block("fig-1", "mermaid", "graph TD; A-->B", "flow")]
    assert graphics.next_graphic_id(history) == "fig-2"
    # Gaps / out-of-order ids: one past the highest, not the count.
    history.append(_graphic_block("fig-5", "svg", "<svg/>", "x"))
    assert graphics.next_graphic_id(history) == "fig-6"


def test_find_graphic_and_all_ids():
    history = [
        _graphic_block("fig-1", "mermaid", "graph TD; A-->B", "flow"),
        _graphic_block("fig-2", "svg", "<svg/>", "logo"),
    ]
    assert graphics.all_graphic_ids(history) == ["fig-1", "fig-2"]
    found = graphics.find_graphic(history, "fig-2")
    assert found == {"id": "fig-2", "format": "svg",
                     "source": "<svg/>", "summary": "logo"}
    assert graphics.find_graphic(history, "fig-9") is None


def test_redact_history_graphics_slims_source_but_keeps_id_and_summary():
    history = [_graphic_block("fig-1", "mermaid", "graph TD; A-->B", "the flow")]
    out = graphics.redact_history_graphics(history)
    # The original history is untouched — full source still there for persistence.
    assert history[0]["content"][0]["input"]["source"] == "graph TD; A-->B"
    inp = out[0]["content"][0]["input"]
    assert "graph TD" not in inp["source"]          # source slimmed away
    assert inp["graphic_id"] == "fig-1"             # id retained
    assert inp["summary"] == "the flow"             # summary retained
    assert "fig-1" in inp["source"] and "GetGraphic" in inp["source"]


def test_redact_history_keeps_loaded_source_only_in_last_message():
    loaded = graphics.loaded_source_result("fig-1", "mermaid", "graph TD; A-->B")
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
# CreateGraphics is a client tool: the controller validates, registers the
# graphic (id + cleaned source kept in history), emits a `graphic` CoreEvent for
# the frontend, and hands the model a tiny tool_result naming the id. GetGraphic
# reloads a stored source by id.


class _RecordingBackend:
    """Stand-in for the real backend: stores registered graphics in a tiny dict
    so the controller's register/get round-trip can be exercised."""

    conversations_dir = None

    def __init__(self):
        self.responses = []
        self.graphics = {}   # id -> {format, source, summary}
        self._seq = 0

    def submit(self, event: BackendEvent):
        if event.kind == "tool_send_response":
            self.responses.append(event)

    def register_graphic(self, tool_use_id, source, summary=None):
        self._seq += 1
        gid = f"fig-{self._seq}"
        # Format is unknown to this stub; the controller test sets it via the
        # graphic event, so store source/summary only.
        self.graphics[gid] = {"source": source, "summary": summary}
        return gid

    def get_graphic(self, graphic_id):
        g = self.graphics.get(graphic_id)
        if not g:
            return None
        return {"id": graphic_id, "format": "vega-lite",
                "source": g["source"], "summary": g["summary"]}

    def graphic_ids(self):
        return list(self.graphics)


def _graphics_controller():
    backend = _RecordingBackend()
    events = []
    controller = ConversationController(
        backend=backend,
        tool_gateway=object(),  # never touched — CreateGraphics is client-side
        worker_spawner=lambda fn: None,
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


def test_valid_graphic_registers_and_reports_id():
    controller, backend, events = _graphics_controller()
    spec = '{"mark": "bar", "encoding": {}}'

    _fire_tool(controller, "CreateGraphics", {
        "format": "vega-lite", "source": spec, "summary": "weekly spend",
    })

    # A `graphic` event carrying the full spec and its id reaches the frontend.
    graphic = next(e for e in events if e.kind == "graphic")
    assert graphic.payload == {
        "format": "vega-lite", "summary": "weekly spend",
        "source": spec, "id": "fig-1",
    }
    # The source was registered into history (not redacted away).
    assert backend.graphics["fig-1"]["source"] == spec
    # The model's result names the id and points at GetGraphic for edits — and
    # never echoes the spec back.
    assert len(backend.responses) == 1
    result = backend.responses[0].tool_result
    assert "fig-1" in result and "GetGraphic" in result and spec not in result


def test_fenced_vega_source_is_cleaned_before_render():
    controller, backend, events = _graphics_controller()

    _fire_tool(controller, "CreateGraphics", {
        "format": "vega-lite",
        "source": '```json\n{"mark": "bar", "encoding": {}}\n```',
        "summary": "spend",
    })

    graphic = next(e for e in events if e.kind == "graphic")
    # The code fence is stripped, so the frontend (and history) get clean JSON.
    assert graphic.payload["source"] == '{"mark": "bar", "encoding": {}}'
    assert backend.graphics["fig-1"]["source"] == '{"mark": "bar", "encoding": {}}'


def test_invalid_graphic_is_handed_back_without_render_or_register():
    controller, backend, events = _graphics_controller()

    _fire_tool(controller, "CreateGraphics", {
        "format": "vega-lite", "source": "{not json", "summary": "x",
    })

    # Nothing rendered, nothing registered — the model must fix and retry.
    assert not any(e.kind == "graphic" for e in events)
    assert backend.graphics == {}
    assert len(backend.responses) == 1
    result = backend.responses[0].tool_result
    assert "not rendered" in result.lower()
    assert "CreateGraphics again" in result


def test_get_graphic_returns_stored_source():
    controller, backend, events = _graphics_controller()
    spec = '{"mark": "line", "encoding": {}}'
    _fire_tool(controller, "CreateGraphics",
               {"format": "vega-lite", "source": spec, "summary": "trend"})

    _fire_tool(controller, "GetGraphic", {"id": "fig-1"}, tool_use_id="g2")

    result = backend.responses[-1].tool_result
    assert spec in result                      # full source handed back to edit
    assert "fig-1" in result and "CreateGraphics" in result


def test_get_graphic_unknown_id_lists_available():
    controller, backend, events = _graphics_controller()
    _fire_tool(controller, "CreateGraphics",
               {"format": "vega-lite", "source": '{"mark": "bar"}',
                "summary": "s"})

    _fire_tool(controller, "GetGraphic", {"id": "fig-9"}, tool_use_id="g2")

    result = backend.responses[-1].tool_result
    assert "fig-9" in result and "fig-1" in result  # not found + what does exist
