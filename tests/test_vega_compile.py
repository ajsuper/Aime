"""Tests for the authoritative Vega-Lite compile gate (aime.vega_compile) and the
compile-tested recipe library (aime.graphics_examples).

The gate runs the same vega-lite compile + vega parse the browser runs, so a spec
that is valid JSON but structurally broken is rejected server-side and handed back
for a same-turn fix instead of failing silently in the client. It's optional — a
box without Node/deps falls back to the loose check — so the compile-exercising
tests skip when it isn't installed. The recipe test is the anti-rot guard: every
skeleton the model is told to copy must itself pass the gate.
"""

import json

import pytest

from aime import graphics_examples
from aime import vega_compile

_needs_compiler = pytest.mark.skipif(
    not vega_compile.available(),
    reason="Node vega-lite compile gate not available",
)


@_needs_compiler
def test_good_spec_compiles():
    good = {
        "mark": "bar",
        "data": {"values": [{"a": "A", "b": 1}]},
        "encoding": {"x": {"field": "a", "type": "nominal"},
                     "y": {"field": "b", "type": "quantitative"}},
    }
    assert vega_compile.compile_error(json.dumps(good)) is None


@_needs_compiler
def test_bad_field_type_is_rejected_with_reason():
    bad = {"mark": "rule", "encoding": {"y": {"datum": 100, "type": "bogusType"}}}
    err = vega_compile.compile_error(json.dumps(bad))
    assert err and "bogusType" in err


@_needs_compiler
def test_every_recipe_compiles_clean():
    # Anti-rot: a recipe the model is told to adapt must pass the same gate
    # CreateGraphics enforces, or the model would copy a spec the server rejects.
    for kind in graphics_examples.KINDS:
        for entry in graphics_examples.get(kind):
            err = vega_compile.compile_error(json.dumps(entry["spec"]))
            assert err is None, f"{kind}: {err}"


def test_fails_open_on_empty_and_oversize():
    # These paths need no subprocess and must never *invent* a rejection: empty
    # source and an absurdly large one both return None (no objection).
    assert vega_compile.compile_error("   ") is None
    assert vega_compile.compile_error("x" * (vega_compile._MAX_SOURCE_CHARS + 1)) is None


def test_kinds_match_library():
    assert vega_compile is not None
    assert graphics_examples.KINDS == tuple(graphics_examples._EXAMPLES.keys())
    for kind in graphics_examples.KINDS:
        entries = graphics_examples.get(kind)
        assert entries and all(
            {"title", "note", "spec"} <= set(e) for e in entries)
    # Unknown / non-str kinds resolve to None, never raise.
    assert graphics_examples.get("nope") is None
    assert graphics_examples.get(None) is None
