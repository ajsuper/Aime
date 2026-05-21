# Data portability & backups

How a user exports, imports, and (in future) auto-backs-up their Aime data.

## What "their data" is

Each user's data lives entirely under `<DATABASE_DIR>/users/<id>/`:

- `database.sql` — calendar events + topic metadata (sqlite)
- `topics/*.md` — topic content files
- `conversations/*.json.enc` — conversation transcripts, encrypted with the
  user's per-account data key (DEK); the AES-GCM AAD is the session id

## The bundle format

Export/import use a single `.zip` bundle:

```
aime-export.json          informational manifest (username, date) — NOT trusted on import
database.sql              copied via sqlite's online backup API (consistent snapshot)
topics/<file>.md          topic content, verbatim
conversations/<id>.json   transcripts, DECRYPTED
```

The manifest is written by the export endpoint as human-readable metadata
only. Import never reads or relies on it: a hand-assembled bundle (e.g.
zipping a local install's data directory) has no manifest, and a manifest
could be forged. Bundles are validated purely by their contents.

The bundle is **unencrypted** — conversations are decrypted on export and
re-encrypted on import. Treat an exported `.zip` as sensitive: it is the
user's data in the clear.

## Export — `GET /account/export`

Streams the bundle as a download. Login required; available regardless of the
user's `api_access` (it is their own data — see [access-control.md](access-control.md)).

## Import — `POST /account/import`

Replaces the logged-in account's data with an uploaded bundle. Multipart form
field `bundle`. Steps:

1. **Validate** — every zip entry must match the known layout, and the bundle
   must contain a `database.sql`. Path-traversal entries (`../`, absolute
   paths) are rejected before anything is written. These guards always run and
   never depend on the manifest.
2. **Back up** — the current data directory is snapshotted first via
   `aime.backup.backup_user_data(reason="import")`. Import is destructive;
   this is the undo.
3. **Detach** — the in-memory `UserContext` is evicted so nothing writes
   during the swap.
4. **Replace** — `database.sql` and `topics/` are overwritten;
   `conversations/*.json` are re-encrypted under *this* account's DEK.
5. **Reload backend** — the C++ backend is told to drop its cached sqlite
   handle (see below).

Import is **replace, not merge** — merging two `database.sql` files would risk
topic/event id collisions. To migrate a separate install, export from it and
import here; the formats are identical and round-trip.

### Backend handle reload

The C++ backend caches an open sqlite handle per user (`g_user_dbs`). After an
import overwrites `database.sql`, that cached handle is stale. The web app
POSTs `{"tool_name": "reload_database", "user_id": N}` to the backend, which
closes and evicts the handle so the next request re-opens the new file. This
becomes unnecessary once the backend is consolidated into Python.

## Backups

`src/aime/backup.py` is a standalone, frontend-agnostic module:

- `backup_user_data(user_id, reason=...)` — snapshot a user's data directory
  into a timestamped zip under `<DATABASE_DIR>/backups/<id>/`. On-disk state is
  copied verbatim (conversations stay encrypted); `database.sql` goes through
  the sqlite backup API for consistency.
- `list_backups(user_id)` — the user's backup zips, oldest first.
- `prune_backups(user_id, keep=N)` — keep only the newest N.

Today this is used only as the pre-import safety net. It is deliberately
schedule-free and reusable: a future autobackup feature can call
`backup_user_data()` on a timer and `prune_backups()` to bound disk use.

## Implementation map

- `src/aime/backup.py` — the backup module.
- `src/frontends/web_app.py` — `/account/export`, `/account/import`, bundle
  validation, the backend-reload call.
- `src/serve.cpp` — the `reload_database` maintenance action.
- `resources/style/web_chat.html` — Export / Import controls in the Account
  modal's Data section.
