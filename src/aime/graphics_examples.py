"""Compile-tested Vega-Lite recipes for the CreateGraphics `LoadGraphicsExamples`
tool.

The model draws basic single-series charts well but gets the *layered* Vega-Lite
constructs wrong from memory — error bands, reference/baseline lines, point
labels, grouped bars, dual axes. This module holds one known-good, adaptable
skeleton per construct: the model loads the one it needs, swaps the inline
`values` for its data, and re-sends it via CreateGraphics — instead of hand-
authoring `layer`/`errorband`/`rule` grammar it recalls imperfectly.

The recipes are context-lean: `LoadGraphicsExamples`' result is stripped from the
model's history after the turn it drew on (graphics.redact_history_graphics), the
same way a reloaded GetGraphic source is — so guidance costs tokens only when
pulled.

Every `spec` here MUST compile through the same gate CreateGraphics uses
(vega_compile.compile_error); tests/test_graphics_examples.py asserts exactly that
so a recipe can never rot into something the model would be told to copy but the
server would then reject.
"""

# Each kind maps to a list of {title, note, spec}. `spec` is a real Vega-Lite
# spec (a dict, serialized to JSON when handed to the model). `note` is the
# one-line adaptation hint — what to change and why the layering is shaped this
# way. Keep specs legible: labelled axes, a title, high-contrast, uncrowded.

_EXAMPLES: dict[str, list[dict]] = {
    "line-multi-series": [{
        "title": "Multiple lines, one per series (colour + legend)",
        "note": ("Put every series in ONE long-format table (a `series` column), "
                 "then `color` by that field — don't hand-place separate lines. "
                 "The legend and palette follow the `color` encoding."),
        "spec": {
            "title": "Weekly active users by plan",
            "data": {"values": [
                {"week": 1, "series": "Free", "users": 120},
                {"week": 2, "series": "Free", "users": 132},
                {"week": 3, "series": "Free", "users": 141},
                {"week": 1, "series": "Pro", "users": 40},
                {"week": 2, "series": "Pro", "users": 55},
                {"week": 3, "series": "Pro", "users": 78},
            ]},
            "mark": {"type": "line", "point": True},
            "encoding": {
                "x": {"field": "week", "type": "quantitative",
                      "title": "Week", "axis": {"tickMinStep": 1}},
                "y": {"field": "users", "type": "quantitative",
                      "title": "Active users"},
                "color": {"field": "series", "type": "nominal", "title": "Plan"},
            },
        },
    }],
    "line-error-band": [{
        "title": "Line with a shaded error / confidence band",
        "note": ("Precompute `lo`/`hi` bounds per point and layer an `errorband` "
                 "(y=lo, y2=hi) UNDER the mean `line`. Precomputed bounds are far "
                 "more reliable than aggregating raw values in-spec. Add a `color` "
                 "field to both layers for multiple banded series."),
        "spec": {
            "title": "Response time (mean ± 95% CI)",
            "data": {"values": [
                {"day": 1, "mean": 210, "lo": 190, "hi": 230},
                {"day": 2, "mean": 205, "lo": 188, "hi": 222},
                {"day": 3, "mean": 230, "lo": 205, "hi": 255},
                {"day": 4, "mean": 218, "lo": 200, "hi": 236},
            ]},
            "encoding": {"x": {"field": "day", "type": "quantitative",
                               "title": "Day", "axis": {"tickMinStep": 1}}},
            "layer": [
                {"mark": {"type": "errorband", "opacity": 0.25},
                 "encoding": {
                     "y": {"field": "lo", "type": "quantitative",
                           "title": "Response time (ms)"},
                     "y2": {"field": "hi"}}},
                {"mark": {"type": "line", "point": True},
                 "encoding": {"y": {"field": "mean", "type": "quantitative"}}},
            ],
        },
    }],
    "reference-line": [{
        "title": "Line chart with a baseline / target reference line",
        "note": ("Layer a `rule` with `y: {datum: <value>}` over your data line "
                 "for a horizontal baseline (use `x` for a vertical one). The "
                 "second text layer labels it — drop that layer if you don't want "
                 "a label."),
        "spec": {
            "title": "Daily signups vs. target",
            "data": {"values": [
                {"day": "Mon", "signups": 82},
                {"day": "Tue", "signups": 95},
                {"day": "Wed", "signups": 78},
                {"day": "Thu", "signups": 110},
                {"day": "Fri", "signups": 91},
            ]},
            "encoding": {"x": {"field": "day", "type": "ordinal",
                               "title": "Day", "sort": None}},
            "layer": [
                {"mark": {"type": "line", "point": True},
                 "encoding": {"y": {"field": "signups", "type": "quantitative",
                                    "title": "Signups"}}},
                {"mark": {"type": "rule", "strokeDash": [4, 4],
                          "color": "#c0392b"},
                 "encoding": {"y": {"datum": 100}}},
                {"mark": {"type": "text", "align": "right", "baseline": "bottom",
                          "dx": -4, "dy": -2, "color": "#c0392b"},
                 "encoding": {
                     "y": {"datum": 100},
                     "x": {"datum": "Fri"},
                     "text": {"value": "Target 100"}}},
            ],
        },
    }],
    "point-labels": [{
        "title": "Points/bars with a value label on each",
        "note": ("Layer a `text` mark over the same encoding as the data mark and "
                 "set `text` to the value field. `dy: -8` lifts labels above "
                 "points; use `dy` positive to sit them below."),
        "spec": {
            "title": "Revenue by quarter",
            "data": {"values": [
                {"q": "Q1", "rev": 12},
                {"q": "Q2", "rev": 18},
                {"q": "Q3", "rev": 15},
                {"q": "Q4", "rev": 24},
            ]},
            "encoding": {
                "x": {"field": "q", "type": "ordinal", "title": "Quarter"},
                "y": {"field": "rev", "type": "quantitative",
                      "title": "Revenue ($M)"},
            },
            "layer": [
                {"mark": {"type": "line", "point": True}},
                {"mark": {"type": "text", "dy": -10, "align": "center"},
                 "encoding": {"text": {"field": "rev", "type": "quantitative",
                                       "format": ".0f"}}},
            ],
        },
    }],
    "grouped-bar": [{
        "title": "Grouped (clustered) bar chart",
        "note": ("Use ONE long-format table and `xOffset` + `color` on the group "
                 "field to cluster bars within each category. For stacked instead "
                 "of grouped, drop `xOffset` and keep `color`."),
        "spec": {
            "title": "Spend by category and month",
            "data": {"values": [
                {"category": "Food", "month": "Jan", "spend": 320},
                {"category": "Food", "month": "Feb", "spend": 290},
                {"category": "Travel", "month": "Jan", "spend": 150},
                {"category": "Travel", "month": "Feb", "spend": 240},
            ]},
            "mark": "bar",
            "encoding": {
                "x": {"field": "category", "type": "nominal", "title": "Category"},
                "xOffset": {"field": "month"},
                "y": {"field": "spend", "type": "quantitative",
                      "title": "Spend ($)"},
                "color": {"field": "month", "type": "nominal", "title": "Month"},
            },
        },
    }],
    "dual-axis": [{
        "title": "Two measures on independent left/right y-axes",
        "note": ("Layer the two marks, give each its own `y` field with an `axis` "
                 "title, and set `resolve.scale.y = independent` so the axes don't "
                 "share a scale. Colour each mark to match its axis."),
        "spec": {
            "title": "Revenue vs. margin",
            "data": {"values": [
                {"month": "Jan", "revenue": 120, "margin": 18},
                {"month": "Feb", "revenue": 145, "margin": 22},
                {"month": "Mar", "revenue": 138, "margin": 20},
                {"month": "Apr", "revenue": 162, "margin": 27},
            ]},
            "encoding": {"x": {"field": "month", "type": "ordinal",
                               "title": "Month", "sort": None}},
            "layer": [
                {"mark": {"type": "bar", "color": "#9AC6E0"},
                 "encoding": {"y": {"field": "revenue", "type": "quantitative",
                                    "axis": {"title": "Revenue ($K)"}}}},
                {"mark": {"type": "line", "point": True, "color": "#c0392b"},
                 "encoding": {"y": {"field": "margin", "type": "quantitative",
                                    "axis": {"title": "Margin (%)"}}}},
            ],
            "resolve": {"scale": {"y": "independent"}},
        },
    }],
    "scatter-trend": [{
        "title": "Scatter plot with a fitted trend line",
        "note": ("Layer a `point` mark with a regression `line` over it. The "
                 "`transform: [{regression}]` sits on the LINE layer only, so the "
                 "points stay raw and the line is the fit."),
        "spec": {
            "title": "Ad spend vs. signups",
            "data": {"values": [
                {"spend": 10, "signups": 22},
                {"spend": 15, "signups": 30},
                {"spend": 22, "signups": 41},
                {"spend": 28, "signups": 46},
                {"spend": 35, "signups": 62},
            ]},
            "layer": [
                {"mark": {"type": "point", "filled": True},
                 "encoding": {
                     "x": {"field": "spend", "type": "quantitative",
                           "title": "Ad spend ($K)"},
                     "y": {"field": "signups", "type": "quantitative",
                           "title": "Signups"}}},
                {"transform": [{"regression": "signups", "on": "spend"}],
                 "mark": {"type": "line", "color": "#c0392b"},
                 "encoding": {
                     "x": {"field": "spend", "type": "quantitative"},
                     "y": {"field": "signups", "type": "quantitative"}}},
            ],
        },
    }],
}

# The kinds offered by the tool, in a stable order (enum in the schema + the
# "did you mean" hint on an unknown kind). Kept in step with the schema's enum.
KINDS: tuple[str, ...] = tuple(_EXAMPLES.keys())


def get(kind: str) -> list[dict] | None:
    """The recipe entries for `kind`, or None if it isn't a known construct.
    Each entry is {title, note, spec} with `spec` a Vega-Lite dict."""
    if not isinstance(kind, str):
        return None
    return _EXAMPLES.get(kind.strip())
