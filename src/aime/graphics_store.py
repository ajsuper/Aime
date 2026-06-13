"""Per-user persistence for graphic assets — the single source of truth.

A *graphic asset* is one drawing the model produced via ``CreateGraphics``: its
``format`` (``vega-lite`` / ``mermaid`` / ``svg``), the full ``source`` that
renders it, and a short ``summary``. This store is what makes a graphic a
first-class, reusable asset rather than a one-shot card: the model creates it
once and then references it by id — ``[graphic:N]`` — anywhere it likes, in a
chat reply or inside a topic body. Chat replay, topic rendering, and shares all
resolve that tag against this one store.

:class:`GraphicStore` is a near-exact clone of
``scheduling.store.ScheduleStore`` / ``agents.AgentDefinitionStore``: one
encrypted file per asset under the user's ``graphics/`` dir, sealed with the
user's DEK and the asset id as AEAD associated data, all IO best-effort (a
failed write returns ``False``; an unreadable file is skipped on listing).

Ids are per-user monotonic — ``graphic-1``, ``graphic-2``, … — allocated one
past the highest already on disk so they stay stable and unique across a user's
whole account (not per-conversation, not per-topic), which is what lets the same
``[graphic:N]`` tag mean the same picture wherever it appears.
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


def _utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def make_graphic_id(n: int) -> str:
    """The canonical asset id for ordinal ``n`` (also the filename stem)."""
    return f"{_ID_PREFIX}{n}"


def graphic_id_ordinal(graphic_id: str) -> int | None:
    """The integer ``N`` inside a ``graphic-N`` id, or None if it isn't one."""
    if not isinstance(graphic_id, str):
        return None
    m = _ID_RE.match(graphic_id)
    return int(m.group(1)) if m else None


class GraphicStore:
    """Reads and writes encrypted graphic assets for one user.

    Mirrors ``ScheduleStore``: one file per asset, encrypted with the user's DEK
    and the asset id as associated data. IO is best-effort so a storage hiccup
    can't take down a request handler — a failed write returns ``False`` and an
    unreadable file is skipped on listing."""

    def __init__(self, graphics_dir: str, dek: bytes):
        self._dir = graphics_dir
        self._dek = dek

    def _path(self, graphic_id: str) -> str:
        return os.path.join(self._dir, f"{graphic_id}{_SUFFIX}")

    def _next_id(self) -> str:
        """One past the highest ``graphic-N`` already on disk. Unreadable or
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
        return make_graphic_id(highest + 1)

    def create(self, fmt: str, source: str, summary: str) -> dict | None:
        """Allocate the next id, persist a fresh asset, and return the stored
        record (``{id, format, source, summary, created_at, updated_at}``). The
        ``source`` should already be ``graphics.normalize``-cleaned by the caller
        (the controller), since this is the canonical copy everything renders
        from. Returns None if the write fails."""
        record = {
            "id": self._next_id(),
            "format": fmt,
            "source": source,
            "summary": summary or "",
            "created_at": _utc_now_iso(),
            "updated_at": _utc_now_iso(),
        }
        return record if self.save(record) else None

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
