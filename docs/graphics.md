# Inline graphics — implementation plan

A `CreateGraphics` tool that lets the model draw directly in the chat: data
charts, flowcharts and diagrams, and the occasional hand-authored mockup. The
graphic renders as a card in the web app where the tool was called, and — this
is the part that makes it affordable — **the bulky source that produced it never
re-enters the model's context after the turn that generated it.**

Builds on the same principle as [[web-search-offload]] and [[document-analysis-plan]]:
heavy content lives *outside* the expensive model's re-cached context. The twist
here is that the model is the *producer* of the heavy content, not the consumer —
so the savings come from a send-time strip, not an offloaded sub-agent.

---

## 0. The core principle

> **The model pays for a graphic's source exactly once — as output tokens on the
> turn it draws it — and never again.** The full source is kept for persistence
> and replay, but the copy sent to the API on every later turn carries only a
> short `summary`.

When the model emits a 3k-token SVG or Vega-Lite spec, that spec is unavoidably
in its *output* for that one turn (it's the producer). What we refuse to pay is
the spec being re-sent as *input* on every subsequent turn via the cached history
prefix — which, for a file-heavy session, is the dominant cost (see
[[conversation-input-cost-findings]]). That re-feed we eliminate entirely.

---

## 1. The tool

A **client tool**, handled in the controller exactly like `WebSearch` /
`SendMessage` (`controller.py` `_handle_tool_use`, ~line 722) — never forwarded
to the C++ data gateway.

Schema — `resources/tools/api_create_graphics_schema.json`:

| Field | Type | Notes |
|---|---|---|
| `format` | `"vega-lite" \| "mermaid" \| "svg"` | required |
| `source` | string | required — the spec / markup. **Stripped from API history after the generation turn.** |
| `summary` | string | required — the *substance*, not "a chart". This is all the model retains, so it must carry the takeaway. |

The three formats own cleanly orthogonal domains, so there's no overlap for the
model to agonize over — the schema description routes it:

| Format | Owns | Why it, not the others |
|---|---|---|
| **Vega-Lite** | data charts: bar, line, scatter, pie/arc, area, histogram | a standard declarative grammar; the model emits *data + an encoding* and the client renders. Well-represented in training data. |
| **Mermaid** | node-edge diagrams: flowchart, sequence, ER, gantt, state | Vega-Lite can't express these; the model is far more reliable emitting `graph TD; A-->B` than hand-placing SVG. |
| **SVG** | mockups, custom illustration, anything pixel-exact | the escape hatch when neither grammar fits. |

On `summary`: a Vega-Lite spec *is* the data the model just computed, so the
summary must capture the result ("weekly spend peaked Tue at $31"), not the
medium ("a bar chart") — otherwise the model forgets the numbers the moment the
source is stripped.

---

## 2. Controller branch — validate, then render or retry

Mirrors the `WebSearch` / `SendMessage` shape in `_handle_tool_use`:

1. **Validate `source` by format:**
   - `vega-lite` → validate against the Vega-Lite JSON Schema (vendored, validated
     with `jsonschema`).
   - `svg` → XML-parse; reject if it won't parse or carries a disallowed
     construct (`<script>`, event handlers, external `href`/`src` — defense in
     depth ahead of the client sanitizer).
   - `mermaid` → **optimistic.** No clean Python parser exists; a malformed spec
     surfaces as a "couldn't render" card client-side rather than a tool error.
2. **On failure** → return the validation error *as the tool_result* (e.g.
   `Invalid Vega-Lite spec: 'encoding.y.field' is required`). The model fixes it
   on the same turn — a self-correcting loop, the way [[friendly-error-messaging]]
   wants failures to be recoverable rather than dead ends.
3. **On success** →
   - emit a `CoreEvent(kind="tool_result", …)` carrying the structured
     `{format, source, summary}` so the frontend can render the card;
   - submit a tiny tool_result back to the model: `Rendered: <summary>`.
     **Never echo `source` back** — that would re-import the bulk we're about to
     strip.

The bulky `source` flows to the UI (and into history for persistence) but the
model only ever gets `summary` in the result.

---

## 3. The context strip — the load-bearing piece

The strip happens at **send-time**, in `_cacheable_messages()`
(`provider_backend.py:1351`) — the single choke point every history→API
conversion already passes through, and which already follows the right pattern:
*shallow-copy the messages, leave `self._messages` untouched, modify only the
copy bound for the API.*

- `self._messages` (persisted to disk, replayed on reload) keeps the **full
  `source`** → graphics survive reload and appear in session replay.
- `_cacheable_messages` calls `graphics.redact_history_graphics`, which walks the
  copy and, for any `tool_use` block whose `name == "CreateGraphics"`, replaces
  `input.source` with a short deterministic placeholder (naming the `fig-N` id +
  summary and pointing at GetGraphic) while preserving `format`, `summary`, and
  `graphic_id`.

Why send-time and **not** mutating `self._messages` in place: that list *is* the
persistence + replay store. Redacting it would strip the source from the saved
session too, breaking the reload-and-replay behavior we want. Keeping the strip
on the API copy gives us both — full fidelity on disk, slim history to the model.

Caching stays intact: the slimmed form is deterministic per turn, so the cached
prefix is stable across the agent loop. The strip is a shallow map — negligible
per-turn cost.

The API does **not** re-validate `tool_use` input against the tool schema on
replay, so a placeholder in a `required` field is fine; we keep `format` +
`summary` populated regardless.

---

## 4. Frontend — the graphic card (`resources/style/web_chat.html`)

The web app already renders model markdown via `marked` + `DOMPurify`
(loaded at lines 7–8) and already builds tool cards from the `tool_result`
event stream. The graphic card hangs off the same stream.

- **Per-format renderer, lazily loaded.** Pull the runtime only when a format
  first appears in a session:
  - `vega-lite` → vega + vega-lite + vega-embed
  - `mermaid` → mermaid.js
  - `svg` → no library
  These are sizeable (vega-embed is hundreds of KB); lazy-load keeps them off the
  initial page weight for sessions that never draw.
- **SVG sanitization.** Run hand-authored SVG through the existing `DOMPurify`
  with an explicit SVG profile — strip `<script>`, event handlers, and external
  references (`<image href>` etc., which are a tracking / exfil vector). This is
  the real trust boundary; the controller's XML check in §2 is defense in depth.
- **Render-failure card.** A spec that won't parse client-side (chiefly Mermaid,
  which we don't validate server-side) shows a calm "couldn't render this
  graphic" card, not a broken element — [[friendly-error-messaging]].
- **Download button** on the card (the topic view already does this for its
  content), exporting the source or a rasterized PNG.

---

## 5. Other frontends

`src/frontends/` TUI and STT can't render any of these. They degrade to a text
placeholder — `[graphic: <summary>]` — built from the same `CoreEvent`, so the
transcript stays readable and the summary still conveys the substance.

---

## 6. Persistence & replay

No new store. The full `source` rides in `self._messages` (§3), which is already
persisted per-session and re-enumerated into the transcript on `/load`. The
strip is invisible to persistence because it lives only on the API-bound copy.

On success the controller calls `backend.register_graphic(tool_use_id, source,
summary)`, which stamps the stored `tool_use` block with its **cleaned** source
(what actually renders, post fence/JSON-repair) and a stable **id** (`fig-N`,
one past the highest already in history). `replay.py` special-cases a
CreateGraphics block, emitting a `graphic` CoreEvent from the stored spec so the
card re-renders on `/load` exactly like a live one (GetGraphic reloads are
skipped — they're internal plumbing, not transcript).

---

## 6a. Editing an existing graphic — the load-back loop

The strip (§3) means that on a later turn the model no longer holds a graphic's
source — only its `fig-N` id and `summary`. So "make that chart green" can't be
answered by editing what's in context; the model would have to redraw from the
summary and get the details wrong. The fix is a companion **`GetGraphic`** client
tool (`api_get_graphic_schema.json`): given a `fig-N` id, it returns that
graphic's full source — read from the copy still in `self._messages` — as the
tool_result, so the model edits the real spec and re-sends it via CreateGraphics.

This preserves the cost guarantee. The source is paid for again only on the
*editing* turn the model deliberately reloads it; `redact_history_graphics` then
slims that reloaded GetGraphic result back down on every later turn — **except**
when it sits in the final message, which is the editing turn that still needs to
read it. So a reload costs the source once, not forever. The CreateGraphics and
GetGraphic tool_results both tell the model the id and steer it to reload-before-
editing rather than redrawing from memory.

A revised graphic is a *new* card with a *new* id (chat is append-only); the old
card stays in the transcript above it.

---

## 7. Cost story (for the dashboard, eventually)

The point of the design is that a graphic's marginal re-cost is **zero** after
generation. Worth surfacing the same way [[document-analysis-plan]] surfaces its
savings: count graphics drawn and the source tokens that *would* have been
re-fed each turn had we not stripped them — i.e. the avoided tax — so the
mechanism's payoff is legible rather than invisible. Shares the savings panel
with [[model-routing-plan]] / [[web-search-offload]].

---

## 8. Implementation order

1. **Schema + config.** `api_create_graphics_schema.json`; add it to the
   interactive tool list in `config.py`. (Decide §9 whether background agents get
   it.)
2. **The strip** (`_cacheable_messages`). Smallest, highest-leverage change, and
   independently testable: assert the API copy carries `summary` not `source`
   while `self._messages` keeps the full spec. Land this before the card so the
   cost guarantee is real from the first render.
3. **Controller branch.** Validation per format, retry-on-failure tool_result,
   success `CoreEvent` + tiny result. Vendor the Vega-Lite schema; wire SVG XML
   parse.
4. **Web card.** Lazy per-format runtimes, DOMPurify SVG profile, failure card,
   download.
5. **TUI/STT fallback** placeholder.
6. **Dashboard counter** (folds into the existing savings panel; not blocking).

---

## 9. Open questions (decide during build, not blocking)

- **Background agents.** Do headless workers ([[background-agents-framework]])
  get `CreateGraphics`? A graphic only renders in an interactive chat; an agent's
  result is delivered via `SubmitResult` / a message. Leaning *interactive-only*
  for v1 — an agent that draws into a void is wasted tokens. Revisit if a use
case (a report-style result with an embedded chart) appears.
- **Vega-Lite schema version** to validate against, and whether to pin it
  vendored vs. fetch — vendored avoids a network dependency in the hot path.
- **Download format**: source-only (smallest, faithful) vs. rasterized PNG
  (shareable, needs a canvas pass per format). Could ship source-only first.
- **Strip placeholder wording.** *Resolved:* the placeholder echoes the `fig-N`
  id + summary and tells the model to reload via GetGraphic before editing —
  enough gist to act on without the bytes, and a pointer to get the bytes back
  when it genuinely needs them.
- **Size cap on `source`.** A guard against a pathological multi-thousand-line
  SVG eating the generation turn's output budget — reject over some ceiling with
  a friendly note, or accept and rely on the strip. Probably a soft cap.
- **Mermaid server-side validation.** Optimistic for v1; if malformed-spec cards
  show up often, revisit (a headless mermaid parse in Node, or a lint pass).
