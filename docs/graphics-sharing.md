# Shared graphics — design spec

How a `CreateGraphics` graphic behaves once topics are **shared** between
accounts. Builds on the implemented inline-graphics feature (`docs/graphics.md`):
the per-user `GraphicStore`, the `[graphic-…]` tag that renders in chat and topic
bodies through one resolver, the send-time source strip, and `GetGraphic`
reload-to-edit. This spec changes **where a graphic lives, what its id looks
like, and who may read or write it** — nothing about how a graphic is drawn,
validated, stripped, or rendered.

> **Status: as-built.** This document matches the shipped implementation,
> including two deviations from the original plan — **absolute ids** (§1) and the
> write rule's **owner-namespace** judging (§3b). See §10 for the file map and the
> one piece left unwired (the topic-delete lifecycle hook, §2).

---

## 0. The core principle

> **A graphic id names a topic; addressing a topic is reuse, identity is
> absolute.** A graphic is scoped to a topic, and *authorization* for that topic
> is the topic layer's job — so we reuse `_resolve_topic` wholesale and never
> build a second ownership system. But the id a graphic is *referred to by* bakes
> the owner in always, so it means the same picture in any context.

A shared topic is content that sits *between* accounts; its graphics are part of
that content, so they are scoped to the topic, not to whoever drew them. Two
concerns pull apart, and we treat them separately:

* **Targeting** — *which* topic to draw into — uses a reader-relative **topic
  handle**, the identical string the model and web layer already use to address
  topics (`T` for an own topic, `O:T` for a shared one, omitted ⇒ personal). So
  "whose store?", "may this user read/write it?" are answered by the topic layer
  (`_parse_topic_handle`, `_resolve_topic` in `web_app.py`). We reuse it.
* **Identity** — the id a graphic is *referenced by* in a tag — is **absolute**:
  the owner is always present (`graphic-<owner>:<topic>:<n>`). This is the one
  deliberate departure from "an id is just a topic handle + ordinal": a topic
  handle is reader-relative (`T` vs `O:T` for the same topic), but an *id* must
  not be, because the model copies it between contexts (a topic body and a chat
  reply) where a reader-relative handle would silently change meaning.

---

## 1. The id grammar

The **emitted** id — what `CreateGraphics` returns and a `[graphic-…]` tag
carries — is always fully qualified:

```
graphic-<owner>:<topic>:<n>     a topic graphic (owner always present)
graphic-0:<n>                   a personal / chat graphic (topic 0, self-only)
```

| Id | Means |
|---|---|
| `graphic-0:<n>` | a chat graphic — `topic 0` = "no topic", always self |
| `graphic-<O>:<T>:<n>` | graphic `n` in owner **O**'s topic **T** |

The owner is **never** omitted for a topic graphic — not even by the topic's own
owner, who gets `graphic-<self>:<T>:<n>`. So the id is **absolute**: the same
string denotes the same picture for everyone, in a topic body *and* in a chat
reply. `_resolve_topic` maps `owner == self → self`, so an owner's own absolute
id resolves to their silo and a recipient's resolves through their grant — the
inline preview works in chat for both, with no reader-relative rewriting.

This is the key fix over a reader-relative id: when the model pulls an id out of a
topic body (e.g. via `GetTopicContents`) and drops it into a chat reply, a bare
`graphic-T:n` would mean "owner O's topic T" in the body but "*my* topic T" in
chat. Baking the owner in removes that ambiguity by construction, rather than
relying on the model to re-qualify the handle per context.

**Targeting stays reader-relative.** The `CreateGraphics` `topic` input is a
topic *handle* (`T` / `O:T`, omit ⇒ personal) — convenient and consistent with
every other tool. The server resolves it to the real `(owner, topic)` and stamps
the **absolute** id from there (`GraphicStore.id_handle`). Input handle in,
absolute id out.

**Back-compat.** A legacy bare `[graphic-N]` (no colon) is read as
`graphic-0:N` — a personal graphic, ordinal N. Older topic bodies may also still
carry an owner's reader-relative `graphic-T:n`; the frontend resolver
(§6) qualifies those against the render context's owner so they keep resolving
*inside the topic body*. Only newly created graphics get absolute ids.

---

## 2. Storage layout

`GraphicStore` becomes **scoped** to `(owner_id, topic_id)`:

| Scope | Directory | Filename stem | AEAD associated data |
|---|---|---|---|
| `topic_id = 0` (personal) | `users/<owner>/graphics/` *(unchanged)* | `graphic-<n>` | `graphic-<n>` |
| `topic_id = T` | `users/<owner>/graphics/topic-<T>/` | `graphic-<n>` | `graphic-<n>` |

Deliberately, **the on-disk stem and AAD stay `graphic-<n>`** — the *directory*
supplies the owner + topic scope; the full id is reconstructed from
`(store scope, n)`. Two payoffs:

* **Zero file migration.** Existing personal graphics already live at
  `users/<id>/graphics/graphic-N.json.enc` with AAD `graphic-N`; they decrypt
  unchanged and are simply reinterpreted as `graphic-0:N`.
* **Bytes are bound to their physical slot** (AAD = stem), so a file can't be
  silently relocated between stores and still decrypt.

The DEK is always the **owner's** (`get_dek(owner_id)`): a topic physically lives
in its owner's silo, so its graphics do too. A recipient never holds the owner's
key — the server writes on their behalf, exactly as topic *text* edits already
do (`topic_shares.py:11-16`). We do **not** give topics their own shared keys;
that would wreck the "revoke = delete a row, no key to rotate" model.

**Lifecycle.** Deleting topic T should delete `…/graphics/topic-<T>/`. The
cleanup helper exists — `_delete_topic_graphics(owner_id, topic_id)` in
`web_app.py`, the companion to `revoke_all_for_topic` — but **is not yet wired**:
there is currently no topic-deletion choke point in the Python layer to hang it
on (deletion is handled in the C++ backend, and `revoke_all_for_topic` itself is
not yet called either). An orphaned topic graphics dir is harmless (encrypted,
owner-scoped, unreferenced once the topic is gone); wire the helper in wherever a
topic-delete hook lands. Un-sharing removes a grant row and touches no graphics —
they were never in anyone's personal store to strip. A declined or purged
recipient likewise leaves the topic's graphics intact.

---

## 3. Resolution = topic resolution

A graphic id is parsed by splitting the trailing `:<n>`; **the remaining prefix
is a topic handle, fed to the existing topic-handle machinery.** One helper:

```
parse_graphic_id("graphic-<handle>:<n>") -> (handle, n)   # legacy bare -> ("0", N)
# then, for both reads and creation:
_resolve_topic(handle, need)            -> (owner, topic)   # request thread (g)
_resolve_topic_as(user_id, handle, …)   -> (owner, topic)   # g-free core
#   handle "0"      -> (self, 0)            personal, self-only
#   handle "T"      -> (self, T)            my own topic
#   handle "O:T"    -> (O, T) iff accepted [+edit] grant, else 403
```

Because `_resolve_topic` already (a) maps a bare handle to *self* (so an owner's
own absolute id `graphic-<self>:T:n` resolves to their silo), (b) verifies an
accepted grant for an `O:T` handle, and (c) checks edit rights when asked, the
graphic layer gets owner-routing **and** IDOR-proof authorization for free. The
personal case (`topic 0`) is self-only by construction: an `O:0` handle from a
non-self user finds no shared "topic 0" and is rejected — personal graphics are
never cross-account because only topics are shared.

(`_resolve_topic_as` is the g-free core extracted so the store provider — called
from a worker thread during a model turn — can resolve without a Flask request
context; `_resolve_topic` is the thin `g.user_id` wrapper for the routes.)

### 3a. Read rule

`GET /graphics/<id>`: `parse_graphic_id` → `_resolve_topic(handle, "view")` →
open the `(owner, topic)` store under `get_dek(owner)` → return `{format, source,
summary}`. Anything that doesn't resolve (no grant, missing file, malformed id)
→ **404**, which the client already degrades to a calm "couldn't load" card.

### 3b. Write rule — a topic body may reference only *its own* graphics

When a topic body is saved (UI `topic_contents_save`, or a model-driven
`ReplaceTopicContents` / `EditTopicContents`), the save already resolves the
target topic to `(O, T)`. Scan the body for `[graphic-…]` tags and **reject the
save unless every tag denotes that same `(O, T)`** — judging each tag *exactly as
it renders* (`graphics_store.tag_handle_scope(handle, O)`), **not** by the
saver's identity:

* `graphic-O':T':n` (explicit) ⇒ `(O', T')` — accepted iff `== (O, T)`.
* `graphic-T:n` (bare, legacy / owner-written) ⇒ `(O, T)` — a bare handle belongs
  to the **topic's owner**, so it denotes this topic iff `T` is this topic. This
  is what lets a **recipient save a shared body verbatim**: the owner's stored
  tags are bare (`graphic-T:n`), and the recipient must be able to keep them.
* `graphic-0:n` (personal) or any other topic's id ⇒ rejected.

> **Why owner-namespace, not saver-identity.** The body is shared *verbatim* and
> stored in the owner's relative form; a recipient is handed that body to edit. If
> the rule resolved a bare `graphic-T:n` as the *saver*, a recipient could never
> save the body they were given (the bare tag would resolve to *their* topic T and
> be rejected). Judging bare tags in the topic-owner namespace makes the write
> rule **consistent with the renderer** (§6, `ownerContext = topic owner`) and
> still rejects every foreign tag, because acceptance requires `== (O, T)`.

Note that with absolute ids (§1) a freshly created tag is already
`graphic-O:T:n`, so the explicit branch is the common path; the bare branch
exists for older content and for an owner's own reader-relative tags.

This guarantees: no mixed ownership, nothing foreign to dangle on un-share, and
the read rule's "topic access authorizes all of a topic's graphics" is *provably*
true because every embedded graphic is provably that topic's.

The deliberate cost: a chat graphic (`topic 0`) can't be embedded into a topic
directly — it must be (re)created *into* the topic (§4, the promote flow). That's
the price of clean lifecycle and zero-lookup auth.

---

## 4. Creating a graphic — `CreateGraphics(…, topic?)`

`CreateGraphics` gains one optional **target** field: `topic`, a topic **handle**
(`T` for an own topic, `O:T` for a shared one; omit ⇒ personal chat graphic).
Reader-relative input, consistent with every other tool. The graphic can be
written **only** to that target; any mismatch fails.

The controller resolves the target through an injected **store provider** rather
than reaching across the trust boundary itself (it has no business holding
`_share_store` / `get_dek`). `UserContext` builds the provider
(`_make_graphic_store_provider`) as a closure capturing the acting user — safe to
call off the request thread (the controller runs on a worker), so it uses the
g-free `_resolve_topic_as`, not `_resolve_topic`:

```
provider(handle, need="edit") -> GraphicStore | None
   _resolve_topic_as(user_id, handle, need) -> (owner, topic)   # raises -> None
   return GraphicStore(graphics_dir(owner), get_dek(owner), owner, topic)
```

In `_handle_create_graphics`:

1. Read `topic` (default `"0"`); get the store via the provider, `need="edit"`.
2. Provider returns `None` (view-only on a shared topic, unshared/forged handle) →
   friendly result steering to leave `topic` empty or use the right handle — no
   store write, recoverable per `friendly-error-messaging`.
3. Otherwise create; the store allocates `n`, and the controller stamps the
   **absolute** id — `format_graphic_id(store.id_handle, n)`, i.e.
   `graphic-<owner>:<topic>:<n>` (or `graphic-0:<n>` for chat) — via
   `register_graphic`, and the result tells the model to write that whole id as
   `[graphic-<owner>:<topic>:<n>]`. The owner is baked in (§1), so the model can
   place that tag in chat *or* the topic body and it resolves identically.

The shared-topic write is the only "bridge," and it is **not a new concept** — it
is the same operation as a recipient editing the topic's *text*: after the same
accepted-edit check, the server performs the write into the owner's silo on the
recipient's behalf.

**Promote a chat graphic into a topic.** Because of the write rule (§3b), "save
that chart to my topic" is: `GetGraphic` the chat id to reload its source, then
`CreateGraphics` with `topic = <handle>` and that source — yielding a fresh
graphic owned by the topic. The CreateGraphics/GetGraphic result text steers the
model here rather than embedding a `graphic-0:n` id (which the save would reject).

---

## 5. Reloading & editing — `GetGraphic`

`GetGraphic`'s `id` is the full absolute id. The controller `parse_graphic_id`s
it and fetches through the **same provider** (so a recipient can reload a shared
topic's graphic to edit it, but only with an accepted-edit grant). A revised
graphic is saved back into the *same* store, getting a new `n` (chat stays
append-only; the old card remains above). The result echoes the absolute id —
`format_graphic_id(store.id_handle, n)` — so a revise round-trip always carries
the owner-qualified form even if the model passed a looser one. The "known ids"
hint on a miss lists the resolved store in its `id_handle` form.

The send-time strip (`redact_history_graphics` in `graphics.py`) is unchanged:
it keys off the `CreateGraphics` tool name + the stamped `graphic_id`, which is
simply the longer id now. Determinism — and therefore prompt caching — holds.

---

## 6. The resolver (frontend)

`resolveGraphicTags(root, ownerId)` (`web_chat.html`) takes the **render
context's owner** and broadens its token regex to the full grammar plus the
legacy bare form (one combined alternation):

```
/\[(graphic-(?:\d+:){1,2}\d+|graphic-\d+)\]/g   // graphic-[O:]T:n  |  legacy bare
```

For **new** content every tag is already absolute (`graphic-O:T:n`), so it
fetches verbatim regardless of context — that is the whole point of §1. The
`ownerContext` path exists only for **older / owner-written reader-relative**
tags (`normalizeGraphicId`):

* **Chat** bubbles resolve with `ownerContext = self` (the default). A bare
  `graphic-T:n` is treated as *my* topic T; an absolute `graphic-O:T:n` resolves
  cross-account (the inline preview) — authorized by the route via the share.
* **Topic** bodies resolve with `ownerContext = the topic's owner`
  (`topicHandleOwner(selectedTopicId)`), so a bare `graphic-T:n` an owner wrote
  before absolute ids resolves to `(O, T)` *inside the body*. An already-absolute
  tag (length-3 handle) is left untouched.

It still walks **text nodes only** and fetches `/graphics/<id>` via
`fetchGraphicAsset` — the only changes are which strings count as a tag and that a
reader-relative bare tag is normalized to its absolute form using `ownerContext`
before the request, so the route always sees a fully-qualified handle.

---

## 7. Migration & back-compat

* **Files:** none to move — personal stores already match the new on-disk shape
  (§2); they become `graphic-0:N`.
* **Tags in old content:** a bare `[graphic-N]` is read as `graphic-0:N`, safe
  because bare ids only ever appear in content authored by and for that same user
  (the old feature was strictly per-user). New content always writes the absolute
  id.
* **Reader-relative tags from the interim design:** a topic body authored while
  ids were still reader-relative may carry an owner's bare `graphic-T:n`. The
  resolver's `ownerContext` (§6) keeps these rendering *inside the topic body*,
  and the write rule's owner-namespace judging (§3b) keeps such a body saveable.
  They will **not** resolve if copied into a chat reply — recreate the graphic to
  get an absolute id. (No bulk migration; these are few and self-healing.)
* **History stamps & replay:** old sessions stamped bare ids and replay resolves
  them via the bare→`0:N` path; new sessions stamp the full absolute id. The
  send-time strip is id-agnostic, so determinism / prompt caching holds.

---

## 8. Duplicates & races

* **Id-allocation race (the real one).** A naive read-max-then-write `_next_n` is
  nearly safe for a single-writer personal store but races in a **shared topic
  store with multiple writers** (owner + edit-recipients). Done: `create`
  allocates atomically — write the new file with `O_CREAT|O_EXCL`
  (`open(path, "xb")`); on `FileExistsError`, recompute `n` and retry (bounded by
  `_MAX_ALLOC_RETRIES`). The loser simply takes the next ordinal — no lock,
  correct across processes (the web sidecar may run several). Shared topics also
  carry a cooperative edit-lock that serializes the common case, but that's
  advisory; `O_EXCL` is the hard backstop.
* **Duplicate content** (same chart drawn twice → two ids): not deduped; cheap
  and harmless. Revisit with a content hash only if it shows up.
* **Topic duplication/copy** (if such a feature exists): a copied body still
  references the *source* topic's graphics, which the write rule (§3b) would now
  reject on save. So topic-copy must run a graphic-copy pass (clone each source
  graphic into the new topic's store and rewrite the tags). Flagged as a
  dependency, not built here.

---

## 9. What does *not* change

The drawing pipeline is untouched: formats and validation (`graphics.py`
`normalize` / `validate`), the per-format card renderers and SVG sanitization
(`web_chat.html`), the send-time strip and reload-to-edit economics, the TUI/STT
text fallback, and "creating a graphic does not display it — the model places the
tag." Background agents and the TUI still get no store/provider, so
`CreateGraphics` degrades to its existing "not available here" result.

---

## 10. Implementation status

All landed (`src/aime/graphics_store.py`, `graphics.py`, `controller.py`,
`src/frontends/web_app.py`, `resources/style/web_chat.html`,
`resources/tools/api_*graphic*_schema.json`), with unit tests in
`tests/test_graphics_store.py` and `tests/test_graphics.py`:

1. **`GraphicStore` scoping + atomic allocation.** Constructor
   `(graphics_dir, dek, owner_id, topic_id)`; `topic-<T>/` nesting; id helpers
   (`parse_graphic_id`, `format_graphic_id`, `tag_handle_scope`, `id_handle`
   property); `O_EXCL` create-and-retry.
2. **Store provider** — `_make_graphic_store_provider` in `UserContext` (just
   `_resolve_topic_as` + a scoped store), injected as `graphic_store_provider`.
3. **Controller branches.** `CreateGraphics` `topic` arg + provider, **absolute**
   id stamping; `GetGraphic` id-parse + provider, absolute echo; promote-flow
   steering in result text.
4. **Write rule** in the topic-save paths (UI `topic_contents_save` +
   model-driven `ReplaceTopicContents` / `EditTopicContents` in the controller) —
   reject tags that don't denote the saved topic, judging bare tags in the
   **topic-owner** namespace (`tag_handle_scope`), not the saver's identity.
5. **Read route** — `parse_graphic_id` + `_resolve_topic("view")`; legacy
   bare→`0:N`; 404 on anything unresolved.
6. **Resolver** — `ownerId` param, broadened regex, `normalizeGraphicId` for
   reader-relative legacy tags before fetch.
7. **Schemas** — `api_create_graphics_schema.json` (`topic` handle; absolute-id
   examples), `api_get_graphic_schema.json` (absolute-id example).
8. **Lifecycle hook** — `_delete_topic_graphics` helper exists but is **not wired**
   (no Python topic-delete choke point yet; see §2 Lifecycle).

Two deliberate deviations from the original spec, both folded into the sections
above: **ids are absolute** (§1, owner always baked in) rather than reader-
relative, and the **write rule judges bare tags by the topic owner** (§3b) rather
than the saver — the original reader-relative scheme is precisely what let a
model-copied id flip meaning between a topic body and a chat reply.
