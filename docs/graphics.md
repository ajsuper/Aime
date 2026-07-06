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
- **The freshest graphic is left intact** (a 2-message keep-recent window): a
  graphic just drawn sits one message back, so its continuation turn reads the
  *real* source it produced. Without this the model sees a placeholder where its
  own `source` argument was and misreads it as a stub it sent by mistake — then
  "resends" with an apology. The placeholder is also voiced as an explicit
  `[system: …]` elision that affirms the source was correct, not as a terse stub,
  so the rare older glance-back doesn't trip the same reflex either.

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

## 6. Persistence, the asset store, and replay

A graphic is a **reusable asset**, not a one-shot card: the model draws it once
and can then place it — by its id, `[graphic-N]` — in a chat reply *or* a topic
body, as many times as it likes. That makes the canonical home a dedicated
per-user **`GraphicStore`** (`aime/graphics_store.py`), a near-exact clone of
`ScheduleStore`/`AgentDefinitionStore`: one DEK-encrypted file per asset under
`users/<id>/graphics/` (id as AEAD associated data, atomic tmp-then-`os.replace`,
best-effort IO). Ids are per-user monotonic — `graphic-1`, `graphic-2`, … —
allocated one past the highest on disk, so the same tag means the same picture
account-wide (not per-conversation, not per-topic). This is **not** in serve.cpp
(the C++ gateway) — same Python-sidecar pattern as the other stores.

On success the controller saves the cleaned source to the store (which allocates
the id), then calls `backend.register_graphic(tool_use_id, graphic_id, source,
summary)` to *stamp* the history `tool_use` block with that id + cleaned source.
The store is the source of truth; the history stamp exists only so the §3 strip
can reference the id (and as a redundant on-disk copy). The full `source` still
rides in `self._messages` (persisted per-session); the strip is invisible to
persistence because it lives only on the API-bound copy. Because chat is
append-only and edits make a *new* id, the store copy and the stamp never drift.

Replay is **tag-driven**: a graphic re-renders on `/load` from the `[graphic-N]`
tag the model wrote into its own assistant text (resolved against the store, §6b),
not from the tool_call — so `replay.py` skips both CreateGraphics and GetGraphic
`tool_use` blocks as internal plumbing, and the asset shows up exactly where the
model placed its tag.

---

## 6a. Editing an existing graphic — the load-back loop

The strip (§3) means that on a later turn the model no longer holds a graphic's
source — only its `fig-N` id and `summary`. So "make that chart green" can't be
answered by editing what's in context; the model would have to redraw from the
summary and get the details wrong. The fix is a companion **`GetGraphic`** client
tool (`api_get_graphic_schema.json`): given a `graphic-N` id, it returns that
graphic's full source — read from the **`GraphicStore`** (§6) — as the
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

## 6b. The `[graphic-N]` tag — one render path for chat and topics

Because a graphic is a stored asset, the model displays it the same way
everywhere: it writes the tag **`[graphic-N]`** in its text, and the tag renders
inline wherever it appears. **Creating a graphic does not display it** — the
`CreateGraphics` tool only saves the asset and returns the id; the model then
writes `[graphic-N]` in its reply (chat) and/or a topic body to show it. One tag,
one resolver (`resolveGraphicTags`), one store — no separate card path, so chat
and topics behave identically and a graphic can never render twice.

- **Chat.** The frontend resolves `[graphic-N]` in the model's assistant text the
  moment a bubble's HTML is finalized — the typewriter lock-in for live turns, the
  direct `innerHTML` set for replay/history. Chat HTML is rendered server-side, so
  the tag arrives as literal text and `resolveGraphicTags` swaps it in place.
- **Topics.** The model writes `[graphic-N]` into a topic body (authored via
  `replace_topic_contents` / `create_topic`). `renderTopicMarkdown` runs the same
  resolver *after* `marked` + `DOMPurify`.

In both surfaces `resolveGraphicTags` walks **text nodes only** (never re-parsing
HTML, so it can't reintroduce unsafe markup): each `[graphic-N]` token is swapped
for a card whose source is fetched from `GET /graphics/<id>` and rendered through
`renderGraphicInto(body, fmt, source)`. A stale/forbidden id degrades to a calm
"couldn't load" card.
- **The route.** `GET /graphics/<id>` (`web_app.py`, `@login_required`) returns
  `{format, source, summary}` from the user's `GraphicStore`. **Owner-scoped:** a
  user reads only their own assets; missing reads as 404. Cross-user resolution
  for a graphic embedded in a *shared* topic routes through the `topic_shares`
  owner bridge (server-side `get_dek(owner_id)`, only for ids referenced in the
  shared body) — see [[scheduling-pipeline-plan]]'s sibling trust boundary; this
  is the remaining slice, not yet wired.

Why text-node-only resolution and a fetch (not inlining the source into the topic
markdown): the body stays small and the bulky source never lives in two places —
the store is the single source of truth, and an edited graphic (new id) can't
leave a stale copy baked into a topic.

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
- **Vega-Lite validation.** *Resolved (see §11).* Instead of a vendored JSON
  schema, CreateGraphics now runs the authoritative **compile gate** — the same
  vega-lite compile + vega parse the browser runs — in a Node subprocess pinned to
  the browser's major, so a structurally-broken spec is rejected with the real
  reason and a same-turn retry, and a `LoadGraphicsExamples` tool serves compile-
  tested skeletons for the harder layered constructs.
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
  show up often, revisit (a headless mermaid parse in Node, or a lint pass). The
  Node harness added in §11 for Vega-Lite makes this cheaper to add later.

---

## 11. The Vega-Lite compile gate + examples tool (as-built)

The model draws basic single-series charts well but gets *layered* Vega-Lite
wrong from memory — error bands, `rule` reference lines, multi-series `color`,
`text` labels, grouped bars, dual axes. Three gaps let those fail invisibly: the
cheap `graphics.validate` can't tell a well-formed spec from a structurally-broken
one; a broken-but-JSON-valid spec was told *"Saved"* and then failed only as a
client-side "Couldn't render" card the model never saw (no retry); and the model
got no examples for the hard constructs. Three coordinated pieces close them:

- **The compile gate** (`aime/vega_compile.py` + `resources/vega/compile.mjs`).
  After `validate` passes, CreateGraphics runs the *same* two steps the browser
  runs before it can draw — `vegaLite.compile` → `vega.parse` — in a short-lived
  Node subprocess pinned (via `package.json`) to the browser's major (`@5`). A
  spec that won't compile comes back through the **existing** retry path with the
  real reason, so self-correction now fires for exactly the failing cases. **Fails
  open:** if Node/deps are absent or it times out, `compile_error` returns None and
  CreateGraphics falls back to the loose gate — the gate only ever *adds*
  rejections. `node_modules` is gitignored; run `npm install` at deploy.

- **`LoadGraphicsExamples`** (`aime/graphics_examples.py` +
  `api_load_graphics_examples_schema.json`). A client tool, dispatched like
  `GetGraphic`, that returns a compile-tested, adaptable skeleton for a requested
  `kind` (multi-series line, error band, reference line, point labels, grouped
  bar, dual axis, scatter trend). Its result is **stripped from history after the
  drawing turn** — reusing `redact_history_graphics` via a second opener sentinel —
  so the guidance costs tokens only when pulled. A test compiles every recipe
  through the gate so the library can't rot into specs the server would reject.

- **The schema pointer.** `api_create_graphics_schema.json` tells the model to
  call `LoadGraphicsExamples` first for anything beyond a simple single series,
  and that specs are now compiled before saving.

Parity, not perfection: `compile`+`parse` catches structural/spec errors — the
dominant failure mode — but not every runtime/data quirk or aesthetic crowding;
the recipes and `vegaThemeConfig` carry the legibility side.
