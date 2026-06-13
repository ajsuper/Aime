# Shared graphics — design spec

How a `CreateGraphics` graphic behaves once topics are **shared** between
accounts. Builds on the implemented inline-graphics feature (`docs/graphics.md`):
the per-user `GraphicStore`, the `[graphic-…]` tag that renders in chat and topic
bodies through one resolver, the send-time source strip, and `GetGraphic`
reload-to-edit. This spec changes **where a graphic lives, what its id looks
like, and who may read or write it** — nothing about how a graphic is drawn,
validated, stripped, or rendered.

---

## 0. The core principle

> **A graphic id is a topic handle plus an ordinal.** Everything before the final
> `:n` is *exactly* a topic handle — the same string the model and the web layer
> already use to address topics — so a graphic inherits topics' addressing,
> ownership, and authorization wholesale. There is no second ownership system.

A shared topic is content that sits *between* accounts; its graphics are part of
that content, so they are scoped to the topic, not to whoever drew them. Pinning
the graphic id to the topic-handle grammar means every hard question —
"whose store?", "may this user read it?", "may this user write it?" — is already
answered by the topic layer (`_parse_topic_handle`, `_resolve_topic` in
`web_app.py:3517`). We reuse it; we don't rebuild it.

---

## 1. The id grammar

```
graphic-<topic-handle>:<n>
```

where `<topic-handle>` is the *identical* grammar topics already use:

| Id | Means | Topic-handle analogue |
|---|---|---|
| `graphic-0:<n>` | my chat graphic — `topic_id 0` = "no topic" | — (personal) |
| `graphic-<T>:<n>` | a graphic in **my** topic T | `T` (own topic) |
| `graphic-<O>:<T>:<n>` | a graphic in owner **O**'s topic T | `O:T` (shared topic) |

So `owner_id` is **optional** (omitted ⇒ self), `topic_id` is **required**
(`0` ⇒ not attached to any topic), and `:n` is the per-store ordinal, always
present. The same picture is `graphic-T:5` to its owner and `graphic-O:T:5` to a
recipient — exactly as the same topic is `T` to its owner and `O:T` to a
recipient. The id is **relative to who reads it**, just like a topic handle.

This is why the inline preview works everywhere: because a recipient *can* name
the owner explicitly (`graphic-O:T:5`), that tag resolves in the recipient's own
chat, not just inside the topic body — without an owner field there'd be no way to
say "O's, not mine," and cross-account references in chat would be impossible.

**Back-compat.** A legacy bare `[graphic-N]` (no colon) is read as
`graphic-0:N` — a personal graphic, ordinal N. Existing chats and topics keep
resolving; only newly written tags carry the handle prefix.

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

**Lifecycle.** Deleting topic T deletes `…/graphics/topic-<T>/` (alongside the
existing `revoke_all_for_topic`). Un-sharing removes a grant row and touches no
graphics — they were never in anyone's personal store to strip. A declined or
purged recipient likewise leaves the topic's graphics intact.

---

## 3. Resolution = topic resolution

A graphic id is parsed by splitting the trailing `:<n>`; **the remaining prefix
is fed to the existing topic-handle machinery.** One helper:

```
parse_graphic_id("graphic-<handle>:<n>") -> (handle, n)
# then, for both reads and the write rule:
_resolve_topic(handle, need) -> (effective_owner_id, topic_id)   # reused verbatim
#   handle "0"      -> (self, 0)            personal, self-only
#   handle "T"      -> (self, T)            my own topic
#   handle "O:T"    -> (O, T) iff accepted [+edit] grant, else 403
```

Because `_resolve_topic` already (a) maps a bare handle to *self*, (b) verifies an
accepted grant for an `O:T` handle, and (c) checks edit rights when asked, the
graphic layer gets owner-routing **and** IDOR-proof authorization for free. The
personal case (`topic_id 0`) is self-only by construction: an `O:0` handle from a
non-self user finds no shared "topic 0" and is rejected — personal graphics are
never cross-account because only topics are shared.

### 3a. Read rule

`GET /graphics/<id>`: `parse_graphic_id` → `_resolve_topic(handle, "view")` →
open the `(owner, topic)` store under `get_dek(owner)` → return `{format, source,
summary}`. Anything that doesn't resolve (no grant, missing file, malformed id)
→ **404**, which the client already degrades to a calm "couldn't load" card.

### 3b. Write rule — a topic body may reference only *its own* graphics

When a topic body is saved (UI `topic_contents_save:4104`, or a model-driven
`replace_topic_contents` / `edit_topic_contents` / `create_topic`), the save
already resolves the target topic to `(O, T)`. Scan the body for `[graphic-…]`
tags and **reject the save unless every tag resolves to that same `(O, T)`** —
using the *saver's* identity, exactly as `_resolve_topic` would:

* `graphic-T:n` (bare) ⇒ `(saver, T)` — accepted only if the saver **is** the
  owner and `T` is this topic. So a recipient editing shared topic `O:T` **must**
  write `graphic-O:T:n`; a bare `graphic-T:n` from them would resolve to *their*
  topic T and is correctly rejected — the same reason they address the topic as
  `O:T` everywhere.
* `graphic-O:T:n` ⇒ `(O, T)` — accepted iff it equals this topic.
* `graphic-0:n` (personal) or any other topic's id ⇒ rejected.

This is "you can only write a graphic into a topic if its handle matches that
topic." It guarantees: no mixed ownership, nothing foreign to dangle on un-share,
and the read rule's "topic access authorizes all of a topic's graphics" is
*provably* true because every embedded graphic is provably that topic's.

The deliberate cost: a chat graphic (`topic 0`) can't be embedded into a topic
directly — it must be (re)created *into* the topic (§4, the promote flow). That's
the price of clean lifecycle and zero-lookup auth.

---

## 4. Creating a graphic — `CreateGraphics(owner_id?, topic_id, …)`

`CreateGraphics` gains the **target**: `topic_id` (required; `0` for a chat
graphic) and `owner_id` (optional; defaults to self). Equivalently — and
consistently with every other tool — this is a single optional `topic` **handle**
field (`T` or `O:T`; omit ⇒ personal `0`). The graphic can be written **only** to
that target; any mismatch fails.

The controller resolves the target through an injected **store provider** rather
than reaching across the trust boundary itself (it has no business holding
`_share_store` / `get_dek`). `_context_for(user_id)` builds the provider as a
closure capturing the acting user, and it is just `_resolve_topic` plus a store:

```
provider(handle, need="edit") -> GraphicStore | None
   _resolve_topic(handle, need) -> (owner, topic)   # raises -> None (unauthorized)
   return GraphicStore(dir_for(owner, topic), get_dek(owner), owner, topic)
```

In `_handle_create_graphics` (`controller.py:911`):

1. Read the target; get the store via the provider with `need="edit"`.
2. Provider returns `None` (e.g. view-only on a shared topic) → friendly result:
   *"You have view-only access to that topic, so I can't add a graphic to it."* —
   no store write, recoverable per `friendly-error-messaging`.
3. Otherwise create as today; the store allocates `n`, the controller stamps the
   full id via `register_graphic`, and the result tells the model to write
   `[graphic-<handle>:<n>]` (the handle echoed back in the same self/shared form
   the model supplied).

The shared-topic write is the only "bridge," and it is **not a new concept** — it
is the same operation as a recipient editing the topic's *text*: after the same
accepted-edit check, the server performs the write into the owner's silo on the
recipient's behalf.

**Promote a chat graphic into a topic.** Because of the write rule (§3b), "save
that chart to my topic" is: `GetGraphic` the chat id to reload its source, then
`CreateGraphics` with `topic = <handle>` and that source — yielding a fresh
graphic owned by the topic. The CreateGraphics/GetGraphic result text steers the
model here rather than embedding a `topic-0` id (which the save would reject).

---

## 5. Reloading & editing — `GetGraphic`

`GetGraphic`'s `id` is the full id. The controller `parse_graphic_id`s it and
fetches through the **same provider** (so a recipient can reload a shared topic's
graphic to edit it, but only with an accepted-edit grant). A revised graphic is
saved back into the *same* store, getting a new `n` (chat stays append-only; the
old card remains above). The "known ids" hint on a miss lists from the resolved
store.

The send-time strip (`redact_history_graphics`, `graphics.py:265`) is unchanged:
it keys off the `CreateGraphics` tool name + the stamped `graphic_id`, which is
simply the longer id now. Determinism — and therefore prompt caching — holds.

---

## 6. The resolver (frontend)

`resolveGraphicTags` (`web_chat.html:3776`) takes the **render context's owner**
so it can resolve a bare (self) handle, and broadens its token regex to the new
grammar plus the legacy bare form:

```
/\[(graphic-(?:\d+:)?\d+:\d+)\]/g   // graphic-[O:]T:n
/\[(graphic-\d+)\]/g                // legacy bare -> graphic-0:N
```

* **Chat** bubbles resolve with `ownerContext = self`. A bare `graphic-T:n`
  means *my* topic T; an explicit `graphic-O:T:n` resolves cross-account (the
  inline preview) — authorized by the route via the share.
* **Topic** bodies resolve with `ownerContext = the topic's owner`, so a bare
  `graphic-T:n` written by the owner resolves to `(O, T)` just like the explicit
  form a recipient writes.

It still walks **text nodes only** and fetches `/graphics/<id>` via
`fetchGraphicAsset` — the only changes are which strings count as a tag and that
the fetched id is normalized to its explicit form using `ownerContext` before the
request, so the route always sees a fully-qualified handle.

---

## 7. Migration & back-compat

* **Files:** none to move — personal stores already match the new on-disk shape
  (§2); they become `graphic-0:N`.
* **Tags in old content:** a bare `[graphic-N]` is read as `graphic-0:N`, safe
  because bare ids only ever appear in content authored by and for that same user
  (the old feature was strictly per-user). New content always writes the
  handle-qualified form.
* **History stamps & replay:** old sessions stamped bare ids and replay resolves
  them via the bare→`0:N` path; new sessions stamp the full id.

---

## 8. Duplicates & races

* **Id-allocation race (the real one).** `_next_id` is read-max-then-write
  (`graphics_store.py:70`) — nearly safe for a single-writer personal store, but
  it races in a **shared topic store with multiple writers** (owner +
  edit-recipients). Fix: make allocation atomic — write the new file with
  `O_CREAT|O_EXCL` (`open(path, "xb")`); on `FileExistsError`, recompute `n` and
  retry (bounded). The loser simply takes the next ordinal — no lock, correct
  across processes (the web sidecar may run several). Shared topics also carry a
  cooperative edit-lock (`topic_partners`, `topic_shares.py:315`) that serializes
  the common case, but that's advisory; `O_EXCL` is the hard backstop.
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

## 10. Implementation order

1. **`GraphicStore` scoping + atomic allocation.** Constructor takes
   `(root, dek, owner_id, topic_id)`; directory + id helpers (`parse_graphic_id`,
   id formatting); `O_EXCL` create-and-retry. Independently unit-testable.
2. **Store provider** in `web_app._context_for` (just `_resolve_topic` + a scoped
   store) and inject into the controller context.
3. **Controller branches.** `CreateGraphics` target arg + provider; `GetGraphic`
   id-parse + provider; promote-flow steering in result text.
4. **Write rule** in the topic-save path(s) — reject tags that don't resolve to
   the saved topic (from the saver's identity).
5. **Read route** — `parse_graphic_id` + `_resolve_topic("view")`; legacy
   bare→`0:N`.
6. **Resolver** — owner-context param, broadened regex, normalize-to-explicit
   before fetch.
7. **Schemas** — `api_create_graphics_schema.json` (target: `topic` handle /
   `owner_id?`+`topic_id`), `api_get_graphic_schema.json` (full-id example).
8. **Lifecycle hook** — delete `topic-<T>/` on topic delete.

Land 1 before everything (it's the substrate, testable in isolation); 4 and 5 are
the security-bearing pair and should land together so the write rule and read
auth are never half-applied.
