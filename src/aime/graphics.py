"""Validation for the CreateGraphics client tool.

UI-agnostic, like `tool_formatting`: returns a plain error string (or None) so
the controller can hand a failed spec straight back to the model as a
tool_result for a same-turn retry, while the frontend owns the actual rendering.

This is a cheap gate, not a renderer. The browser's vega-embed / mermaid /
DOMPurify layer is the authoritative boundary — these checks just catch the
obvious mistakes early (so the model gets a clean error to fix) and keep
script-bearing SVG from ever reaching the page. Vega-Lite is checked for
JSON-validity and spec shape rather than against the full Vega-Lite JSON Schema;
the client surfaces deeper spec errors as a render-failure card.
"""

import json
import re
import xml.etree.ElementTree as ET

ALLOWED_FORMATS = ("vega-lite", "mermaid", "svg")

# Top-level keys that mark a dict as an actual Vega-Lite spec rather than a bare
# data dump — a spec needs a mark (and usually an encoding) or a composition
# container.
_VEGA_SPEC_KEYS = (
    "mark", "layer", "encoding", "hconcat", "vconcat",
    "concat", "facet", "repeat", "spec",
)

# Cheap defense-in-depth for hand-authored SVG. The frontend sanitizer
# (DOMPurify, SVG profile) is the real boundary; these reject the blatant cases
# up front so a script/handler-bearing spec never even renders.
_SVG_SCRIPT_RE = re.compile(r"<\s*script", re.IGNORECASE)
_SVG_HANDLER_RE = re.compile(r"\son\w+\s*=", re.IGNORECASE)
_SVG_JS_URI_RE = re.compile(r"javascript:", re.IGNORECASE)


def validate(fmt: str, source: str) -> str | None:
    """Return a human-readable error if (fmt, source) can't be rendered, else
    None. The error is written *for the model* — name the problem plainly so it
    can fix the spec and call CreateGraphics again."""
    if fmt not in ALLOWED_FORMATS:
        return (f"Unknown format {fmt!r}. Use one of: "
                f"{', '.join(ALLOWED_FORMATS)}.")
    if not isinstance(source, str) or not source.strip():
        return "The `source` is empty. Provide the graphic spec/markup."

    if fmt == "vega-lite":
        try:
            spec = json.loads(source)
        except ValueError as exc:
            return f"Invalid Vega-Lite JSON: {exc}."
        if not isinstance(spec, dict):
            return "A Vega-Lite spec must be a JSON object, not a list or value."
        if not any(k in spec for k in _VEGA_SPEC_KEYS):
            return ("This doesn't look like a Vega-Lite spec — it needs a "
                    "`mark` (and usually an `encoding`), or a layer/concat/"
                    "facet/repeat container.")
        return None

    if fmt == "svg":
        if (_SVG_SCRIPT_RE.search(source)
                or _SVG_HANDLER_RE.search(source)
                or _SVG_JS_URI_RE.search(source)):
            return ("The SVG contains a script, an event handler, or a "
                    "javascript: URI. Graphics must be static — remove these.")
        try:
            ET.fromstring(source)
        except ET.ParseError as exc:
            return f"The SVG isn't well-formed XML: {exc}."
        return None

    # mermaid — optimistic: no clean Python validator exists, so the client
    # renders it and surfaces a failure card if the diagram text is malformed.
    return None
