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

# A single surrounding markdown code fence (```lang … ```). Models frequently
# wrap a spec in one even though the field wants raw markup; strip it so the
# parse/render sees clean content.
_FENCE_RE = re.compile(r"^\s*```[\w-]*[ \t]*\r?\n?(.*?)\r?\n?\s*```\s*$", re.DOTALL)

# A trailing comma before a closing `}`/`]` — the single most common way a model
# produces JSON that every strict parser rejects. We strip these *only* as a
# fallback when the spec doesn't parse as-is (see `normalize`), so we never touch
# a string that legitimately contains `,}`.
_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")

# Recognised Mermaid diagram-type openers (lower-cased, no whitespace). Mermaid
# has no Python parser, so the gate below is deliberately loose: it rejects only
# source whose first real line names no diagram type at all — prose pasted by
# mistake, or a missing `flowchart TD` / `sequenceDiagram` opener. That case is
# the main way a Mermaid graphic fails silently (the client shows a failure card
# but the model, already told "rendered", never learns to retry). Anything that
# names a real type passes straight through to the browser renderer.
_MERMAID_TYPES = frozenset({
    "graph", "flowchart", "sequencediagram", "classdiagram", "statediagram",
    "erdiagram", "journey", "gantt", "pie", "quadrantchart",
    "requirementdiagram", "gitgraph", "mindmap", "timeline", "zenuml",
    "sankey", "xychart", "block", "packet", "kanban", "architecture",
    "radar", "treemap", "c4context", "c4container", "c4component",
    "c4dynamic", "c4deployment",
})
# Splits the opening keyword off the first content line: stop at whitespace or
# any of the punctuation a diagram type can be immediately followed by.
_MERMAID_TOKEN_RE = re.compile(r"[\s:;(){}\[\]]")


def strip_code_fence(source: str) -> str:
    """Drop a single surrounding ```…``` code fence (and outer whitespace) if the
    model wrapped its spec in one. Returns the source trimmed but otherwise
    unchanged when there's no fence."""
    if not isinstance(source, str):
        return source
    m = _FENCE_RE.match(source)
    return m.group(1).strip() if m else source.strip()


def normalize(fmt: str, source: str) -> str:
    """Clean up the common, harmless ways a model malforms a spec so it renders
    instead of erroring — then hand the cleaned source to *both* validation and
    the browser (the controller renders what this returns).

    Currently: strip a surrounding code fence (all formats), and, for Vega-Lite,
    drop trailing commas when — and only when — doing so turns unparseable JSON
    into parseable JSON. The narrow guard keeps us from ever altering a spec that
    was already valid."""
    if not isinstance(source, str):
        return source
    source = strip_code_fence(source)
    if fmt == "vega-lite":
        try:
            json.loads(source)
        except ValueError:
            cleaned = _TRAILING_COMMA_RE.sub(r"\1", source)
            if cleaned != source:
                try:
                    json.loads(cleaned)
                    source = cleaned  # adopt only because it now parses
                except ValueError:
                    pass
    return source


def _svg_for_parse(source: str) -> str:
    """A copy of the SVG safe to hand to ElementTree: bind any well-known
    namespace prefix the markup uses but doesn't declare (chiefly `xlink:`, a
    common older-SVG idiom). Without this, ElementTree rejects a perfectly
    renderable drawing with an 'unbound prefix' error the browser would shrug
    off. Only the validation copy is touched; the original is what renders."""
    if "xlink:" in source and "xmlns:xlink" not in source:
        return re.sub(
            r"<svg\b",
            '<svg xmlns:xlink="http://www.w3.org/1999/xlink"',
            source, count=1, flags=re.IGNORECASE,
        )
    return source


def _mermaid_keyword(source: str) -> str | None:
    """The diagram-type token that opens a Mermaid spec (lower-cased), skipping a
    leading YAML frontmatter block, comments, and `%%{init}%%` directives. None
    when there's no content line at all."""
    lines = source.splitlines()
    i = 0
    if i < len(lines) and lines[i].strip() == "---":  # --- frontmatter --- block
        i += 1
        while i < len(lines) and lines[i].strip() != "---":
            i += 1
        i += 1  # step past the closing ---
    for line in lines[i:]:
        s = line.strip()
        if not s or s.startswith("%%"):  # blank, comment, or %%{...}%% directive
            continue
        return _MERMAID_TOKEN_RE.split(s, maxsplit=1)[0].lower()
    return None


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
            return (
                f"Invalid Vega-Lite JSON ({exc}). The `source` must be a single "
                "raw JSON object — start it with `{`, end with `}`, no markdown "
                "code fences, no comments, and no text around it."
            )
        if not isinstance(spec, dict):
            return ("A Vega-Lite spec must be a JSON object starting with `{` "
                    "(a `mark` plus an `encoding`), not a bare array or value. "
                    "Wrap your data and channels in a spec object.")
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
            root = ET.fromstring(_svg_for_parse(source))
        except ET.ParseError as exc:
            return (f"The SVG isn't well-formed XML: {exc}. Check that every "
                    "tag is closed and every attribute value is quoted.")
        if root.tag.split("}")[-1].lower() != "svg":
            local = root.tag.split("}")[-1]
            return (f"The root element is <{local}>, not <svg>. Wrap the whole "
                    "drawing in a single <svg …> element with a `viewBox`.")
        return None

    # mermaid — no Python parser exists, so this is a loose gate, not a real
    # validator: we reject only source that names no diagram type (the client
    # renders the rest and shows a failure card if the body is malformed).
    keyword = _mermaid_keyword(source)
    if keyword is None:
        return "The Mermaid `source` has no diagram content to render."
    if keyword not in _MERMAID_TYPES and keyword.split("-")[0] not in _MERMAID_TYPES:
        return (
            f"{keyword!r} isn't a Mermaid diagram type, so this won't render. "
            "Begin the diagram with its type on the first line — e.g. "
            "`flowchart TD`, `sequenceDiagram`, `classDiagram`, "
            "`stateDiagram-v2`, `erDiagram`, `gantt`, or `pie`."
        )
    return None


# --- History persistence and the context strip -----------------------------
# A graphic's canonical copy lives in the user's GraphicStore (graphics_store.py),
# keyed by a per-user id (`graphic-1`, `graphic-2`, …). A *stamped copy* of the
# cleaned source + that id also rides in the message history, for two reasons:
# replay re-renders a session's graphics from it on /load, and the source-strip
# below references the id. But the model never pays for that source per-turn —
# the strip replaces it with a short placeholder on the copy sent to the API
# (the model pays for the source once, as output, on the turn it draws it), and
# reloads the real source from the store via GetGraphic only when it edits.

GRAPHIC_TOOL_NAME = "CreateGraphics"
GET_GRAPHIC_TOOL_NAME = "GetGraphic"

# A `[graphic-…]` tag in a chat reply or topic body. Matches the addressable
# grammar — `graphic-<handle>:<n>` where the handle is `T` or `O:T` — plus the
# legacy bare `graphic-N` (read as personal `graphic-0:N`). Kept in step with the
# frontend resolver's regex (web_chat.html) and graphics_store.parse_graphic_id.
_GRAPHIC_TAG_HANDLE_RE = re.compile(r"\[graphic-((?:\d+:){1,2}\d+|\d+)\]")


def graphic_tag_handles(text: str) -> list[str]:
    """Every topic handle referenced by a `[graphic-…]` tag in `text`, in order
    (duplicates kept). The handle is the part *before* the trailing `:n`: a bare
    legacy `[graphic-N]` yields personal `"0"`, `[graphic-T:n]` yields `"T"`, and
    `[graphic-O:T:n]` yields `"O:T"`. Used by the topic write rule to check every
    embedded graphic belongs to the topic being saved."""
    out: list[str] = []
    for m in _GRAPHIC_TAG_HANDLE_RE.finditer(text or ""):
        parts = m.group(1).split(":")
        out.append("0" if len(parts) == 1 else ":".join(parts[:-1]))
    return out


def foreign_graphic_tag_message(handle: str) -> str:
    """Friendly refusal when a topic body embeds a graphic that isn't its own.
    Steers toward the fix — (re)create the graphic into this topic — rather than
    naming an internal rule (see friendly-error-messaging)."""
    return (
        f"This topic can't show [graphic-{handle}:…]: a topic only displays "
        "graphics that were created in it. Make the graphic in this topic first "
        "(CreateGraphics with this topic's handle, or reload the original with "
        "GetGraphic and re-create it here), then place its new tag."
    )

# Opening line of a GetGraphic result that carries a reloaded source. Doubles as
# the sentinel the strip matches to slim that result back down once the editing
# turn it was loaded for has passed.
_LOADED_SOURCE_OPENER = "[Loaded source of "


def loaded_source_result(graphic_id: str, fmt: str, source: str) -> str:
    """The tool_result GetGraphic hands the model: the full source, opened with a
    line that both instructs the model and lets the strip recognise it later."""
    return (
        f"{_LOADED_SOURCE_OPENER}{graphic_id} — edit it and call CreateGraphics "
        f"to update the graphic.]\nformat: {fmt}\n\n{source}"
    )


def _graphic_placeholder(graphic_id: str, summary: str) -> str:
    # This stands in for the model's *own* prior `source` argument, so the wording
    # matters: a terse "[placeholder]" reads to the model like it mistakenly sent
    # a stub and triggers a spurious "oops, let me resend" retry. So we (a) tag it
    # unmistakably as a system action, not the model's content; (b) affirm that
    # the source it sent was correct and rendered; (c) avoid priming words like
    # "placeholder"/"don't apologize". The freshest graphic is left intact by
    # redact_history_graphics, so this is only ever read at conversational
    # distance, where the system framing lands cleanly.
    cap = f' — "{summary}"' if summary else ""
    return (
        f"[system: the source you sent for {graphic_id}{cap} was correct and "
        "rendered to the user; it is kept on file and trimmed from this "
        f'transcript to save space. To revise it, call GetGraphic("{graphic_id}") '
        "to reload the exact source, then edit and re-send it.]"
    )


_LOADED_SOURCE_SLIMMED = (
    "[A graphic's source was loaded here earlier and is omitted now to save "
    "context. Call GetGraphic again if you still need it.]"
)


def _result_carries_loaded_source(block) -> bool:
    content = block.get("content")
    return isinstance(content, str) and content.startswith(_LOADED_SOURCE_OPENER)


# Keep the most recently drawn graphic's source intact this many messages back.
# A freshly rendered graphic sits in the second-to-last message (its tool_result
# is last), so a window of 2 lets the continuation turn read the *real* source it
# just produced — which is what stops the model from misreading the placeholder
# as a stub it sent by mistake and "resending". Older graphics fall outside the
# window and are slimmed normally.
_KEEP_RECENT_GRAPHIC_MESSAGES = 2


def redact_history_graphics(messages):
    """Return a copy of `messages` slimmed for sending to the model: every
    CreateGraphics `source` becomes a short placeholder (the model keeps the id +
    summary) — *except* the freshest graphic, kept intact through its continuation
    turn (see `_KEEP_RECENT_GRAPHIC_MESSAGES`) so the model never sees a stub
    where the source it just wrote should be. Reloaded GetGraphic sources are
    slimmed too, except one in the final message (the editing turn that needs it).

    Pure and non-mutating: only the touched message/content/block dicts are
    copied, so the caller's persisted `self._messages` is untouched and the
    cached prefix stays byte-stable (every placeholder is deterministic)."""
    last_index = len(messages) - 1
    keep_recent_from = len(messages) - _KEEP_RECENT_GRAPHIC_MESSAGES
    out = list(messages)
    for mi, msg in enumerate(messages):
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        role = msg.get("role")
        new_content = None  # copied lazily, only once we change something
        for bi, block in enumerate(content):
            if not isinstance(block, dict):
                continue
            replacement = None
            if (role == "assistant" and block.get("type") == "tool_use"
                    and block.get("name") == GRAPHIC_TOOL_NAME
                    and mi < keep_recent_from):
                inp = block.get("input")
                if isinstance(inp, dict) and (inp.get("source") or ""):
                    gid = inp.get("graphic_id") or "this graphic"
                    new_inp = {**inp, "source": _graphic_placeholder(
                        gid, inp.get("summary") or "")}
                    replacement = {**block, "input": new_inp}
            elif (role == "user" and block.get("type") == "tool_result"
                    and mi != last_index and _result_carries_loaded_source(block)):
                replacement = {**block, "content": _LOADED_SOURCE_SLIMMED}
            if replacement is not None:
                if new_content is None:
                    new_content = list(content)
                new_content[bi] = replacement
        if new_content is not None:
            out[mi] = {**msg, "content": new_content}
    return out
