"""Tests for the CreateGraphics validator (aime.graphics.validate).

The validator is the server-side gate that decides whether a graphic gets
rendered or handed back to the model as a retry-able error. It's a cheap check —
the browser is the real renderer — so these lock in the obvious accept/reject
cases per format, especially the SVG safety rejections.
"""

from provider_backend import BackendEvent
from aime import graphics
from aime.controller import ConversationController, _GRAPHIC_SOURCE_PLACEHOLDER


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


def test_mermaid_optimistic():
    # No server-side validator for Mermaid — any non-empty text passes the gate.
    assert graphics.validate("mermaid", "graph TD; A-->B") is None
    assert graphics.validate("mermaid", "not really mermaid but non-empty") is None


# --- Controller wiring -----------------------------------------------------
# CreateGraphics is a client tool: the controller validates, emits a `graphic`
# CoreEvent for the frontend, hands the model a tiny tool_result, and strips the
# bulky source from the stored tool_use input (Phase 1).


class _RecordingBackend:
    conversations_dir = None

    def __init__(self):
        self.responses = []
        self.redactions = []

    def submit(self, event: BackendEvent):
        if event.kind == "tool_send_response":
            self.responses.append(event)

    def redact_tool_use_field(self, tool_use_id, field, placeholder=""):
        self.redactions.append((tool_use_id, field, placeholder))
        return True


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


def _fire_graphic(controller, tool_input):
    controller._handle_tool_use(BackendEvent(
        kind="tool_use",
        tool_name="CreateGraphics",
        tool_input=tool_input,
        tool_use_id="g1",
        expects_response=True,
    ))


def test_valid_graphic_emits_event_and_strips_source():
    controller, backend, events = _graphics_controller()
    spec = '{"mark": "bar", "encoding": {}}'

    _fire_graphic(controller, {
        "format": "vega-lite", "source": spec, "summary": "weekly spend",
    })

    # A `graphic` event carrying the full spec reaches the frontend.
    graphic = next(e for e in events if e.kind == "graphic")
    assert graphic.payload == {
        "format": "vega-lite", "summary": "weekly spend", "source": spec,
    }
    # The bulky source is stripped from the stored tool_use input.
    assert backend.redactions == [("g1", "source", _GRAPHIC_SOURCE_PLACEHOLDER)]
    # The model gets a tiny result that carries only the summary, not the spec.
    assert len(backend.responses) == 1
    result = backend.responses[0].tool_result
    assert "weekly spend" in result and spec not in result


def test_invalid_graphic_is_handed_back_without_render_or_strip():
    controller, backend, events = _graphics_controller()

    _fire_graphic(controller, {
        "format": "vega-lite", "source": "{not json", "summary": "x",
    })

    # Nothing rendered, nothing stripped — the model must fix and retry.
    assert not any(e.kind == "graphic" for e in events)
    assert backend.redactions == []
    assert len(backend.responses) == 1
    result = backend.responses[0].tool_result
    assert "not rendered" in result.lower()
    assert "CreateGraphics again" in result
