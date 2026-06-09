# Document & image analysis — implementation plan

How Aime ingests an uploaded file or image, makes the model genuinely understand
it, and — critically — keeps the cost proportional to *what the model actively
looks at* rather than *file size × number of turns*.

The problem this solves: today every non-image upload is utf-8-decoded (binary
docs become replacement-character garbage), inlined into the user's message text
up to `_MAX_TEXT_CHARS = 200_000` (~50k tokens), stored verbatim in history, and
**re-read on every subsequent turn** at the 0.1× cache-read rate until compaction
at 32 messages. A user who uploads a large dataset and then asks ten follow-ups
pays for that dataset ten times. That re-read tail is the dominant cost for
file-heavy users.

The fix is not one mechanism but a **router across three pipelines**, because
"understand a document" means three different things — *compute over it*,
*read its meaning*, *see it* — and each has a different right primitive and a
different cost-control story.

Builds on the same principle as [[web-search-offload]]: bulk content lives
*outside* the expensive model's context; only the slice it needs enters, and it
leaves when focus moves on.

---

## 0. The core principle

> **Large content never enters Sonnet's context.** Tabular data lives in a
> sandbox and is *queried*; semantic documents live in a Haiku sub-agent and are
> *cited*; only images and tiny text files are resident, and those are evicted
> on a deterministic schedule.

When that holds, "evict the file on pivot" — the fragile part of every naive
design — mostly disappears, because there is nothing resident to evict. Eviction
shrinks to a bounded problem over images and small inline files only (Pipeline 3).

---

## 1. Ingestion routing — `src/frontends/web_app.py` `/upload`

Routing is **deterministic**, decided at upload time from MIME type + a cheap
content probe + size. No model call. The `/upload` response gains a `pipeline`
field the client echoes back on `/send`, so the backend knows how to mount the
attachment.

| Detected (MIME + probe) | `pipeline` | How it's handled | Extraction |
|---|---|---|---|
| CSV / TSV / XLSX / large JSON | `tabular` | Files API → code-execution sandbox | in-sandbox (`pandas`/`openpyxl`) |
| PDF (born-digital **or** scanned) | `semantic` | native PDF document block into the DocumentAgent | none — PDF blocks are citable natively; scans handled by Haiku vision |
| DOCX / ODT / RTF | `semantic` | `pypandoc` → markdown text block into the DocumentAgent | local (`pypandoc` is already a dep) |
| TXT / MD / source code, **large** | `semantic` | text block into the DocumentAgent | none |
| TXT / MD / source code, **tiny** (< ~2k tokens) | `inline` | inlined into the user message (today's path) | none |
| Screenshot / photo (PNG/JPG/GIF/WebP/HEIC) | `visual` | image block in Sonnet's context | existing `_convert_image` |
| Huge (≫ container or context window) | `tabular` only, or reject | code-exec is the only viable path; if not tabular, friendly cap notice | — |
| zip / audio / video / unknown binary | `reject` | friendly notice ([[friendly-error-messaging]]) | — |

Notes on the probe:
- **Scanned vs born-digital PDF:** attempt a fast text-layer read; if extracted
  text length ≪ page count, treat as scanned (still `semantic`, but the
  DocumentAgent relies on the page images via vision rather than the text layer).
  Either way it is a native PDF block — no branch in our code.
- **"large" vs "tiny" text:** a token estimate (`chars / 4`) gates whether a
  plain-text file is worth a DocumentAgent or just inlined. Tiny files aren't
  worth the round-trip.
- This routing **removes the utf-8-garbage bug**: PDF/xlsx/docx never hit the
  `raw.decode("utf-8", errors="replace")` path.

---

## 2. Pipeline `tabular` — Files API + code execution

For CSV / XLSX / large JSON, the operations are *computational* and fidelity must
be exact. An LLM "reading" 10k rows to compute a mean is wasteful and wrong;
pandas is neither.

**Flow:**
1. `/upload` streams the file to the Anthropic **Files API** → `file_id`,
   persisted in the (encrypted) session record.
2. On the user's turn, the backend mounts a `container_upload` block referencing
   `file_id` and exposes the server-side code-execution tool. Sonnet also gets a
   one-line **schema descriptor** (columns, row count, dtypes) — *not* the rows.
3. "Average revenue by region" → Sonnet writes pandas → runs in the sandbox →
   only the result table returns to context.
4. Follow-ups **reuse the same container** (`response.container.id`, persisted in
   the session; containers live ~30 days), so the data loads once and later
   queries are pure compute.
5. "Extract the Q3 rows into a topic" → `df[df.quarter=="Q3"]` → exact rows
   return → Sonnet calls the existing topic tools with them. Exact values,
   guaranteed.

**Cost:** ≈ 0 context cost regardless of file size or turn count — the data is
never in any model's context, only the sandbox. This is the cheapest pipeline and
the right home for the CSV/xlsx workload.

**Tool surface:** the code-execution tool is *server-side* (Anthropic runs it) and
does **not** route through the C++ tool gateway. It is additive to the existing
tool list, mounted only while a tabular attachment is active (same
append-after-the-cache-breakpoint trick as the terminal tool at
`provider_backend.py:_tools_for_turn`, so toggling it never busts the cached
prefix).

---

## 3. Pipeline `semantic` — the DocumentAgent (Haiku) with citations

This is [[web-search-offload]] reborn for documents. A per-session
`DocumentAgent` (Haiku, model `config.HAIKU_MODEL`) **holds the full document in
its own context, cached**. Sonnet never sees the document — it sees a tool.

Implementation mirrors `src/aime/web_search_agent.py`: a small class holding its
own `Anthropic` client, a deterministic harvest of the response blocks, and a
record-usage callback tagged `purpose="document"`.

### 3.1 The document block (and TTL)

The document lives in the DocumentAgent's message history as **one cached block
with citations enabled**:

```jsonc
{ "role": "user", "content": [
  { "type": "document",
    "source": { /* native PDF, or text block from pandoc/plain text */ },
    "citations": { "enabled": true },
    "cache_control": { "type": "ephemeral", "ttl": "1h" } } ] }
```

**TTL = 1h, not 5m.** The document is byte-stable for the whole session — a
perfect stable prefix, exactly like the system prompt (which already gets 1h at
`provider_backend.py:362`). It is written once and read on every query; the only
question is whether the cache survives the *gap between queries*, and
document-analysis think-time (user reads a multi-paragraph answer, decides the
next question) routinely exceeds the 5m TTL — unlike the 0.1m median *message*
gap. The 1h write premium (2× vs 1.25×) is paid once on the doc-sized write;
re-writing the whole doc every time a query lands after a 5-minute gap is far
worse. Confirm post-launch via the dashboard: filter `purpose=document` and check
the doc-block reuse factor is comfortably > 2.

### 3.2 The `query_document` contract — "Haiku points, the tool copies"

The fidelity rule: **separate *where* from *what*.** Haiku decides *where* the
answer is (model judgment, tolerant of fuzz); deterministic code copies *what's
there* (lossless, never the model). This closes the telephone-game risk that
would otherwise corrupt extraction-to-topics.

Mechanism — **Anthropic Citations** (primary). Because the document block has
`citations: {enabled: true}`, Haiku's response interleaves its own prose with
**citation blocks**, and each carries `cited_text` — the exact source span,
*extracted by the API from the document, not generated by the model*. The tool
harvests `cited_text` deterministically (same shape as
`web_search_agent._harvest`) and hands the verbatim spans to Sonnet. The model
chose *where*; it cannot corrupt *what*.

Tools exposed to Sonnet:

| Tool | Returns | Use |
|---|---|---|
| `query_document(question)` | `{answer, quotes[]}` (below) | the workhorse — targeted questions |
| `outline_document()` | structural map: headings, sections, ~entities, (tabular schema if mixed) | gives Sonnet a map of a doc it has never seen, so it can query a doc it can't see |
| `load_full_document()` | inlines the full text into Sonnet for **one** turn, then evicted | escalation hatch for deep whole-document reasoning where targeted querying is insufficient |

`query_document` return shape:

```jsonc
{ "answer": "<Haiku's framing / synthesis>",
  "quotes": [ { "text": "<verbatim cited_text>", "location": "p.12 §3.2" }, ... ] }
```

The DocumentAgent's **system prompt** enforces the dual mode:
- *Gist questions* ("what does the intro say?") → synthesize into `answer`;
  citing is optional.
- *Factual / procedural / quantitative* ("the instructions for adding X fluid to
  Y mixture", "the termination clause", "the figure in row 14") → **must cite**,
  so the exact text rides back in `quotes[]`. Never paraphrase numbers or
  procedures.

Sonnet uses `answer` for gist and `quotes[]` whenever it will *act* on the
content (store it in a topic, compute against it) — guaranteeing zero drift on
the extract-into-topics workflow.

### 3.3 Guards (mechanism-independent)

- **Max span:** cap any single returned span at ~2–4k tokens. If Haiku cites
  more, return truncated-with-marker and have it narrow. Prevents "cite half the
  document" from re-introducing the bulk into Sonnet's context.
- **Ambiguity:** the citation location disambiguates a span that occurs more than
  once; for the fallback mechanism (below) Haiku returns the section too.
- **Miss handling:** if a query genuinely finds nothing, `answer` says so and
  `quotes[]` is empty — never fabricate.

### 3.4 Fallback mechanism: pre-numbered units

For any format where we'd rather not lean on the citations feature, segment the
document at load time by prefixing each line/paragraph with a stable ID
(`[¶42] …`). Haiku then returns **unit ranges** (`"¶42–¶47"`) instead of quote
strings; the tool maps the range back to the clean original text. This dodges the
two failure modes of naive literal-quote anchors — Haiku emits an integer ID it
can *see printed next to the text* (no exact-reproduction problem) and never has
to count characters (offsets are unreliable from an LLM). It is the same
indexed-block idea citations use internally, owned by us. Literal-`find()` anchor
matching is a last resort only.

### 3.5 Why not just put the doc in Sonnet's context (cached)?

Worked economics for a doc of D tokens, session of T turns, Q of which query it:
- **Doc-in-Sonnet (cached):** ≈ `D × $3/Mtok × 0.1 × T` (re-read every turn).
- **DocumentAgent:** ≈ `D × $1/Mtok × 0.1 × Q` (Haiku reads, query turns only).

Two compounding wins: Haiku input is 3× cheaper, and the doc is read only on
query turns (Q), not all turns (T). For a 50k-token doc over 20 turns that's
roughly 300k vs ~60k token-equivalents, and the latter at a third the rate —
~5–10× on the document's contribution. The cost of citation output (short spans,
Haiku output rate) is tracked under `purpose=document` and is far smaller than the
re-read it replaces.

---

## 4. Pipeline `visual` — vision, and the only pipeline that needs eviction

Images must be in the model's context (vision can't be proxied through
Haiku-text). Image-bearing turns already force Sonnet (`model_router.py`, the
`has_images` branch). Screenshots are resolution-capped by `_convert_image`
(≈ 1.15MP / ~1600 tokens), so the per-image cost is bounded; the cost risk is
**accumulation** — many page-images/screenshots staying resident across many
turns.

### 4.1 Deterministic resident-image policy

Replaces "evict on pivot" with count- and age-based rules (no intent inference):

- Keep at most **K = 2** most-recent image blocks resident in history.
- Evict an image when a **(K+1)th** arrives, **or** it is **≥ M = 4 turns old**.
- Eviction = replace the image block in stored history with
  `{"type":"text","text":"[image \"chart.png\" sent earlier — omitted to save context; ask to re-show it]"}`.
  Caption is the filename (free) or a one-line Haiku caption generated once at
  ingest.
- A follow-up needing an evicted image re-attaches it from the encrypted session
  store.

### 4.2 Batch evictions into the compaction pass

Editing a block at history position N invalidates the message-tail cache for
everything ≥ N (`_cacheable_messages` at `provider_backend.py:1351`). Evicting
continuously would bust the cache every turn. So **fold media-eviction into
`_run_compaction_bg`** (`provider_backend.py:1651`) — it already rewrites history
and eats one cache bust; piggyback the image sweep on it. One amortized rewrite,
then every later turn reads the slimmed prefix.

But add a **second trigger independent of message count.** Compaction fires at
`COMPACT_TRIGGER_MSGS = 32`, yet an image-heavy exchange blows the budget by
message 6. Add a **resident-bulk-token threshold** (e.g. resident
image/inline-file tokens > 20k) that triggers a media sweep on its own, separate
from the 32-message count.

### 4.3 Compaction must strip bulk before summarizing

`_summarize` does `json.dumps(old, …)` over the oldest messages
(`provider_backend.py:1623`). If those contain base64 image data, the full base64
string is shipped to Haiku — large and pointless. Replace image/inline-file
blocks with their placeholders **before** the dump. (Fix 4.2 makes this moot for
images that were already swept, but keep the guard for anything compacted before
its sweep.)

---

## 5. Backend & submit() changes — `src/provider_backend.py`

- `BackendEvent` gains an `attachments` field carrying typed mounts:
  `{"pipeline": "tabular"|"semantic"|"visual"|"inline", ...}` (file_id, doc
  handle, or image bytes). Images keep today's path (`submit()` at `:764`).
- `submit()` routes by pipeline: `visual`/`inline` append blocks as today;
  `tabular` records the `file_id` + mounts the code-exec tool for the turn;
  `semantic` registers the doc with the session's DocumentAgent and mounts the
  `query_document` / `outline_document` / `load_full_document` tools.
- The DocumentAgent and code-exec tools, like WebSearch, are **client/offload
  tools the controller dispatches** (DocumentAgent in-process; code-exec is
  server-side so it streams within the same Sonnet turn) — they are not forwarded
  to the C++ data backend.
- `_MAX_TEXT_CHARS = 200_000` becomes the **fallback-only** cap for the `inline`
  pipeline; large content routes to a pipeline instead of inlining, so it rarely
  fires.

---

## 6. Storage, lifecycle & the encryption boundary — `docs/security.md`

Two retention stories, both to be documented:

- **Semantic docs** live in the app: held in the per-session DocumentAgent's
  context, encrypted at rest inside the session blob with the user's DEK, sent to
  Anthropic only for inference (cached). No new at-rest exposure.
- **Tabular files** go to the Anthropic **Files API** (Anthropic-hosted, persists
  until deleted / 30-day container). This is genuinely off-box. Mitigations:
  delete the `file_id` at session end (and on `/reset`), and add a line to
  `docs/security.md`. (Aime already sends all content to Anthropic for inference,
  so this is a *retention* change, not a new trust boundary.)
- **Persistence is session-scoped at launch:** an attachment belongs to the
  session it was uploaded in; a new file replaces the active one. Cross-session
  "keep analyzing yesterday's file" (persist `file_id`/doc handle in the session
  record and re-mount on load) is a noted future extension, deferred for the
  simpler lifecycle and cleaner security story.

---

## 7. Usage tagging — `src/aime/usage.py`, dashboard

- New `purpose="document"` tag on every DocumentAgent call (mirrors
  `purpose="route"` / web_search), so the dashboard surfaces DocumentAgent cost,
  citation-output tokens, and the doc-block reuse factor.
- Code-execution turns carry their existing server-tool accounting; surface the
  container-hour charge if it ever leaves the free tier.
- A small dashboard panel: per-session "document analysis" cost = DocumentAgent +
  code-exec + resident-image tokens, so the file-heavy user that motivated this is
  legible at a glance.

---

## 8. Implementation order

1. **Routing + un-break binaries** (`/upload`): MIME/probe classification, the
   `pipeline` field, reject-unknown, stop utf-8-mangling PDF/xlsx/docx. Immediate
   correctness win even before the pipelines land.
2. **Pipeline `semantic` (DocumentAgent)**: the highest-cost workload. Clone
   `web_search_agent.py`, citations-primary `query_document`, the `{answer,
   quotes[]}` contract, 1h doc-block TTL, `purpose=document` usage. Ship with
   `outline_document`; add `load_full_document` once the base path is proven.
3. **Pipeline `tabular`**: Files API upload, code-exec tool mount, container
   reuse, schema descriptor, session-end cleanup.
4. **Pipeline `visual` eviction**: resident-image policy, fold into
   `_run_compaction_bg`, the bulk-token trigger, strip-before-summarize guard.
5. **Dashboard panel + `_MAX_TEXT_CHARS` downgrade.**

---

## 9. Open questions (decide during build, not blocking)

- **Extraction location for PDFs:** native PDF document block (no extraction, this
  plan's default — citable, vision for scans) vs. extract text in the code-exec
  sandbox and feed a text block to the DocumentAgent. Native is simpler and keeps
  semantic docs off the Files API; revisit only if PDF-block citation quality
  disappoints.
- **`load_full_document` gating:** how Sonnet earns the escalation (explicit
  justification? a per-session budget?) so it doesn't reflexively pull the whole
  doc and defeat the design.
- **DocumentAgent reuse across a multi-file session:** one agent holding several
  docs (cited by document index) vs. one agent per doc. One-per-doc is simpler to
  cache and evict; multi-doc enables cross-document questions.
- **K / M / bulk-token thresholds** (resident-image policy): start at K=2, M=4,
  20k tokens; tune from the dashboard.
- **Citation field shape:** confirm `cited_text` / `char_location` /
  `page_location` field names and the citable-format list against the Citations
  docs before wiring `query_document`'s harvest.
