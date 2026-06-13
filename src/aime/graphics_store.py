"""Persistence for graphic assets — the single source of truth, scoped to a
topic.

A *graphic asset* is one drawing the model produced via ``CreateGraphics``: its
``format`` (``vega-lite`` / ``mermaid`` / ``svg``), the full ``source`` that
renders it, and a short ``summary``. This store is what makes a graphic a
first-class, reusable asset rather than a one-shot card: the model creates it
once and then references it by id — ``[graphic-<handle>:N]`` — anywhere it
likes, in a chat reply or inside a topic body. Chat replay, topic rendering, and
shares all resolve that tag against this one store.

A store is scoped to an ``(owner_id, topic_id)`` pair (see docs/graphics-sharing
.md): personal/chat graphics live in ``users/<owner>/graphics/`` (``topic_id 0``,
the legacy layout, unchanged); a topic's graphics live in
``users/<owner>/graphics/topic-<T>/``. The *directory* supplies the owner+topic
scope, so the on-disk filename stem and AEAD associated data stay the bare
``graphic-<n>`` form — existing personal files decrypt unchanged and are simply
reinterpreted as ``graphic-0:N``. The full, addressable id
(``graphic-<handle>:<n>``) is reconstructed by the caller from the store's scope
plus the ordinal; the store itself only ever deals in bare ordinals on disk.

:class:`GraphicStore` mirrors ``scheduling.store.ScheduleStore`` /
``agents.AgentDefinitionStore``: one encrypted file per asset, sealed with the
owner's DEK and the bare stem as AEAD associated data, all IO best-effort (a
failed write returns ``None``/``False``; an unreadable file is skipped on
listing). Allocation is **atomic** (``O_EXCL`` create-and-retry) so a topic
store with several writers — owner + edit-recipients, possibly across processes
— never hands out the same ordinal twice.
"""

import datetime
import json
import os
import re

from cryptography.exceptions import InvalidTag

from . import encryption as _enc
from . import graphics as _graphics

_SUFFIX = ".json.enc"
_ID_PREFIX = "graphic-"
_ID_RE = re.compile(rf"^{_ID_PREFIX}(\d+)$")
# A topic handle: a bare topic id ``T`` (own / personal ``0``) or an
# owner-qualified ``O:T`` (a shared topic). Used to validate the handle half of
# a full graphic id; the topic layer re-validates it for authorization.
_HANDLE_RE = re.compile(r"^\d+(?::\d+)?$")
# How many times allocation retries when it loses the O_EXCL race for an
# ordinal. Far more than the realistic number of concurrent writers on one
# topic; a runaway just returns None rather than spinning.
_MAX_ALLOC_RETRIES = 50


def _utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def make_graphic_id(n: int) -> str:
    """The bare on-disk stem for ordinal ``n`` (also the AEAD associated data)."""
    return f"{_ID_PREFIX}{n}"


def graphic_id_ordinal(graphic_id: str) -> int | None:
    """The integer ``N`` inside a bare ``graphic-N`` stem, or None if it isn't
    one. (For a full ``graphic-<handle>:N`` id use :func:`parse_graphic_id`.)"""
    if not isinstance(graphic_id, str):
        return None
    m = _ID_RE.match(graphic_id)
    return int(m.group(1)) if m else None


def format_graphic_id(handle: str, n: int) -> str:
    """The full, addressable id for ordinal ``n`` in topic ``handle`` —
    ``graphic-<handle>:<n>``. ``handle`` is a topic handle (``"0"`` personal,
    ``"T"`` an own topic, ``"O:T"`` a shared one); the same picture is thus
    ``graphic-T:n`` to its owner and ``graphic-O:T:n`` to a recipient, exactly
    as a topic is ``T`` vs ``O:T``."""
    return f"{_ID_PREFIX}{handle}:{n}"


def tag_handle_scope(handle: str, topic_owner_id: int) -> tuple[int, int] | None:
    """The ``(owner_id, topic_id)`` a topic-embedded `[graphic-…]` tag denotes,
    given the owner of the topic the tag sits in. The write rule uses this to
    check that an embedded graphic belongs to its topic, interpreting the handle
    *exactly as the renderer does* (ownerContext = the topic's owner):

    * a bare ``"T"`` belongs to the topic's owner — ``(topic_owner_id, T)`` — so
      the owner's own ``graphic-T:n`` and a recipient's view of that same body
      both judge it as "this topic's", just as both render it that way;
    * an explicit ``"O:T"`` names its owner — ``(O, T)``;
    * a personal ``"0"`` (or legacy bare) is never a topic graphic → ``None``.

    Returning the owner-relative scope (not the *saver's*) is what lets a
    recipient save a shared body that still carries the owner's bare tags."""
    parts = str(handle).split(":")
    try:
        if len(parts) == 1:
            t = int(parts[0])
            return None if t == 0 else (topic_owner_id, t)
        if len(parts) == 2:
            return (int(parts[0]), int(parts[1]))
    except ValueError:
        return None
    return None


def parse_graphic_id(graphic_id: str) -> tuple[str, int] | None:
    """Split a full id into ``(handle, n)``, or ``None`` if it isn't a graphic
    id. A legacy bare ``graphic-N`` (no colon) reads as personal ``("0", N)`` so
    old chats and topic bodies keep resolving; ``graphic-<handle>:<n>`` returns
    that handle verbatim for the topic layer to authorize."""
    if not isinstance(graphic_id, str) or not graphic_id.startswith(_ID_PREFIX):
        return None
    rest = graphic_id[len(_ID_PREFIX):]
    if ":" not in rest:
        # Legacy bare graphic-N -> personal graphic-0:N.
        return ("0", int(rest)) if rest.isdigit() else None
    handle, _, n_s = rest.rpartition(":")
    if not n_s.isdigit() or not _HANDLE_RE.match(handle):
        return None
    return (handle, int(n_s))


class GraphicStore:
    """Reads and writes encrypted graphic assets for one user.

    Mirrors ``ScheduleStore``: one file per asset, encrypted with the user's DEK
    and the asset id as associated data. IO is best-effort so a storage hiccup
    can't take down a request handler — a failed write returns ``False`` and an
    unreadable file is skipped on listing."""

    def __init__(self, graphics_dir: str, dek: bytes,
                 owner_id: int | None = None, topic_id: int = 0):
        # The owner's base graphics dir; a topic store nests one level under it
        # so the directory itself carries the (owner, topic) scope. ``topic_id``
        # 0 is the personal/chat store at the base dir (the legacy layout).
        self._dek = dek
        self.owner_id = owner_id
        self.topic_id = topic_id or 0
        self._dir = (graphics_dir if not self.topic_id
                     else os.path.join(graphics_dir, f"topic-{self.topic_id}"))

    @property
    def id_handle(self) -> str:
        """The handle baked into this store's full graphic ids. For a topic store
        it is the **absolute** ``"<owner>:<topic>"`` — the owner is always present
        so the id means the same graphic in any context (a topic body *and* a
        chat reply, for the owner *and* a recipient), with no reader-relative
        reinterpretation. The personal/chat store is ``"0"`` (always self, never
        shared, so never ambiguous)."""
        return "0" if not self.topic_id else f"{self.owner_id}:{self.topic_id}"

    def _path(self, graphic_id: str) -> str:
        return os.path.join(self._dir, f"{graphic_id}{_SUFFIX}")

    def _next_n(self) -> int:
        """One past the highest ordinal already on disk. Unreadable or
        oddly-named files are ignored for the high-water mark — only files that
        match the id pattern count, so a stray file can't break allocation."""
        highest = 0
        try:
            names = os.listdir(self._dir)
        except OSError:
            names = []
        for name in names:
            if not name.endswith(_SUFFIX):
                continue
            n = graphic_id_ordinal(name[: -len(_SUFFIX)])
            if n is not None:
                highest = max(highest, n)
        return highest + 1

    def create(self, fmt: str, source: str, summary: str) -> dict | None:
        """Atomically allocate the next ordinal, persist a fresh asset, and
        return the stored record (``{id, format, source, summary, created_at,
        updated_at}`` — ``id`` is the bare stem; the caller builds the full
        ``graphic-<handle>:<n>`` id from the store's scope). The ``source``
        should already be ``graphics.normalize``-cleaned by the caller (the
        controller), since this is the canonical copy everything renders from.

        Allocation is race-safe across writers and processes: the file is
        created with ``O_EXCL`` (``"xb"``), and on a collision — another writer
        took that ordinal first — the next ordinal is recomputed and retried.
        Returns ``None`` if the format is unusable or the write fails."""
        if fmt not in _graphics.ALLOWED_FORMATS:
            return None
        try:
            os.makedirs(self._dir, exist_ok=True)
        except OSError:
            return None
        for _ in range(_MAX_ALLOC_RETRIES):
            stem = make_graphic_id(self._next_n())
            record = {
                "id": stem,
                "format": fmt,
                "source": source,
                "summary": summary or "",
                "created_at": _utc_now_iso(),
                "updated_at": _utc_now_iso(),
            }
            try:
                blob = _enc.encrypt_blob(
                    self._dek, json.dumps(record).encode("utf-8"),
                    aad=stem.encode("utf-8"))
            except (TypeError, ValueError):
                return None
            try:
                # O_EXCL: we own this ordinal only if the create succeeds; a
                # concurrent writer that got there first raises FileExistsError
                # and we take the next ordinal instead. No lock, correct across
                # processes (the web sidecar may run several).
                with open(self._path(stem), "xb") as f:
                    f.write(blob)
            except FileExistsError:
                continue
            except OSError:
                return None
            return record
        return None

    def save(self, record: dict) -> bool:
        """Persist a record (refreshing ``updated_at``). Returns ``False`` on any
        failure, including a record missing an id or carrying an unusable
        ``format`` — so a malformed asset never reaches disk."""
        graphic_id = record.get("id")
        if not graphic_id or graphic_id_ordinal(graphic_id) is None:
            return False
        if record.get("format") not in _graphics.ALLOWED_FORMATS:
            return False
        record = {**record, "updated_at": _utc_now_iso()}
        try:
            os.makedirs(self._dir, exist_ok=True)
            path = self._path(graphic_id)
            plaintext = json.dumps(record).encode("utf-8")
            blob = _enc.encrypt_blob(self._dek, plaintext, aad=graphic_id.encode("utf-8"))
            tmp = f"{path}.{os.getpid()}.tmp"
            with open(tmp, "wb") as f:
                f.write(blob)
            os.replace(tmp, path)
            return True
        except (OSError, TypeError, ValueError):
            return False

    def load(self, graphic_id: str) -> dict | None:
        """Decrypt and return one asset, or ``None`` if missing/unreadable. An
        AAD mismatch (a tampered or wrong-id file) reads as unreadable."""
        if graphic_id_ordinal(graphic_id) is None:
            return None
        try:
            with open(self._path(graphic_id), "rb") as f:
                blob = f.read()
            plaintext = _enc.decrypt_blob(self._dek, blob, aad=graphic_id.encode("utf-8"))
            return json.loads(plaintext.decode("utf-8"))
        except (OSError, ValueError, InvalidTag):
            return None

    def list_graphics(self) -> list[dict]:
        """Every asset for this user, newest first. Unreadable files are skipped
        rather than failing the listing."""
        try:
            names = os.listdir(self._dir)
        except OSError:
            return []
        out: list[dict] = []
        for name in names:
            if not name.endswith(_SUFFIX):
                continue
            record = self.load(name[: -len(_SUFFIX)])
            if record is not None:
                out.append(record)
        out.sort(key=lambda r: r.get("created_at", ""), reverse=True)
        return out

    def delete(self, graphic_id: str) -> bool:
        """Remove an asset. Returns ``True`` if a file was deleted, ``False`` if
        it didn't exist or couldn't be removed."""
        try:
            os.remove(self._path(graphic_id))
            return True
        except OSError:
            return False
