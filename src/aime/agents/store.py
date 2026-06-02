"""Persistence for background-agent runs.

A run record is deliberately kept *separate* from the user's conversations: a
background agent never appears in the chat history or the /load list. Instead
each run is written here as its own encrypted file, so there is a durable,
auditable trail of what every agent did — its inputs, its result, its full
transcript, and timing.

Storage mirrors the conversation store: one file per run under the user's
``agent_runs/`` directory, encrypted at rest with the same per-user DEK (so run
records get the same protection as conversations), with the run id bound in as
the AEAD associated data. All IO is best-effort: a failed write must never
break the run that produced the record — the result is still returned to the
caller in memory regardless.
"""

import datetime
import json
import os

from .. import encryption as _enc


_SUFFIX = ".json.enc"


def _jsonable(value):
    """Last-resort coercion for anything json.dumps can't handle, so a stray
    object in a transcript can't fail the whole write."""
    return str(value)


class AgentRunStore:
    """Reads and writes encrypted background-agent run records for one user."""

    def __init__(self, runs_dir: str, dek: bytes):
        self._dir = runs_dir
        self._dek = dek

    def _path(self, run_id: str) -> str:
        return os.path.join(self._dir, f"{run_id}{_SUFFIX}")

    def save(self, record: dict) -> bool:
        """Persist a run record (must contain a ``run_id``). Returns True on
        success, False on any failure — callers treat persistence as advisory."""
        run_id = record.get("run_id")
        if not run_id:
            return False
        try:
            os.makedirs(self._dir, exist_ok=True)
            path = self._path(run_id)
            plaintext = json.dumps(record, default=_jsonable).encode("utf-8")
            blob = _enc.encrypt_blob(
                self._dek, plaintext, aad=run_id.encode("utf-8")
            )
            tmp = f"{path}.{os.getpid()}.tmp"
            with open(tmp, "wb") as f:
                f.write(blob)
            os.replace(tmp, path)
            return True
        except (OSError, TypeError, ValueError):
            return False

    def load(self, run_id: str) -> dict | None:
        """Decrypt and return a single run record, or None if missing/unreadable."""
        try:
            with open(self._path(run_id), "rb") as f:
                blob = f.read()
            plaintext = _enc.decrypt_blob(
                self._dek, blob, aad=run_id.encode("utf-8")
            )
            return json.loads(plaintext.decode("utf-8"))
        except (OSError, ValueError):
            return None

    def list_runs(self) -> list[dict]:
        """Lightweight metadata for every stored run, newest first.

        Reads each record (they're small) and projects the summary fields, so a
        future dashboard can list runs without pulling full transcripts into the
        listing. Unreadable files are skipped rather than failing the listing.
        """
        try:
            names = os.listdir(self._dir)
        except OSError:
            return []
        out: list[dict] = []
        for name in names:
            if not name.endswith(_SUFFIX):
                continue
            record = self.load(name[: -len(_SUFFIX)])
            if record is None:
                continue
            out.append({
                "run_id": record.get("run_id", ""),
                "agent_name": record.get("agent_name", ""),
                "status": record.get("status", ""),
                "started_at": record.get("started_at", ""),
                "ended_at": record.get("ended_at", ""),
                "turns": record.get("turns", 0),
            })
        out.sort(key=lambda r: r.get("started_at", ""), reverse=True)
        return out


def new_run_id(agent_name: str) -> str:
    """A sortable, unique run id: ``run-<agent>-<utc timestamp>-<rand>``."""
    import hashlib
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")
    rand = hashlib.sha1(os.urandom(8)).hexdigest()[:8]
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in agent_name)[:40]
    return f"run-{safe}-{stamp}-{rand}"
