"""Persistence for *saved* background-agent definitions.

A background agent's transient form is an ``AgentSpec`` (a value an author fills
in and registers). This module adds the missing piece for end users: a durable,
per-user library of agents they created in the UI, so an agent survives the run
that produced it and can be re-run, edited, deleted — or, eventually, run on a
schedule.

Storage mirrors ``AgentRunStore`` exactly: one encrypted file per definition
under the user's ``agents/`` directory, sealed with the same per-user DEK (so a
definition gets the same protection as conversations and run records), with the
agent id bound in as the AEAD associated data. Keeping definitions beside runs —
rather than in a shared SQL table — means an agent's whole footprint (its
definition and every run it produced) lives inside that one user's directory.

A definition is intentionally close to an ``AgentSpec``: ``definition_to_spec``
builds the spec the runner executes. Scheduling lives elsewhere now — a scheduled
run is a ``run_agent`` record in the schedule store (``aime.scheduling``) keyed by
this agent's id — so a definition holds only the agent's identity and behavior.
"""

import datetime
import hashlib
import json
import os

from cryptography.exceptions import InvalidTag

from .. import encryption as _enc
from .spec import AgentSpec


_SUFFIX = ".json.enc"


def _utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def new_agent_id(name: str) -> str:
    """A stable, unique, filesystem-safe id for a saved agent:
    ``agent-<slug>-<rand>``. The slug is derived from the display name purely
    for human-readability of the on-disk file; uniqueness comes from the random
    suffix, so two agents may share a display name without colliding."""
    slug = "".join(c if c.isalnum() or c in "-_" else "-" for c in name).strip("-")
    slug = (slug[:40] or "agent").lower()
    rand = hashlib.sha1(os.urandom(8)).hexdigest()[:8]
    return f"agent-{slug}-{rand}"


def make_definition(
    *,
    name: str,
    instructions: str,
    description: str = "",
    allow_web_search: bool = False,
    agent_id: str | None = None,
) -> dict:
    """Build a fresh definition record from user-supplied fields, stamping the
    id and timestamps. Scheduling is no longer stored here — a scheduled run is a
    ``run_agent`` record in the schedule store (aime.scheduling) that references
    this agent's id, so an agent can have zero or several schedules independent
    of its definition."""
    now = _utc_now_iso()
    return {
        "agent_id": agent_id or new_agent_id(name),
        "name": name,
        "description": description,
        "instructions": instructions,
        "allow_web_search": bool(allow_web_search),
        "created_at": now,
        "updated_at": now,
    }


def definition_to_spec(record: dict) -> AgentSpec:
    """Build the ``AgentSpec`` the runner executes from a saved definition.

    The spec's ``name`` is the user's display name (the runner sanitizes it for
    the run id, and the run record keeps it as ``agent_name`` so saved-agent runs
    show a friendly label in the runs list). A saved agent gets the full toolset
    and the standard agent model — the same defaults as an ad-hoc run."""
    return AgentSpec(
        name=record.get("name") or record.get("agent_id") or "agent",
        description=record.get("description") or "Saved agent",
        instructions=record.get("instructions") or "",
        allow_web_search=bool(record.get("allow_web_search", False)),
    )


class AgentDefinitionStore:
    """Reads and writes encrypted saved-agent definitions for one user.

    Mirrors ``AgentRunStore``: one file per agent, encrypted with the user's DEK
    and the agent id as associated data. All IO is best-effort — a failed write
    returns False rather than raising, so a storage hiccup can't take down the
    request handling it."""

    def __init__(self, agents_dir: str, dek: bytes):
        self._dir = agents_dir
        self._dek = dek

    def _path(self, agent_id: str) -> str:
        return os.path.join(self._dir, f"{agent_id}{_SUFFIX}")

    def save(self, record: dict) -> bool:
        """Persist a definition (must contain an ``agent_id``), refreshing its
        ``updated_at`` stamp. Returns True on success, False on any failure."""
        agent_id = record.get("agent_id")
        if not agent_id:
            return False
        record = {**record, "updated_at": _utc_now_iso()}
        try:
            os.makedirs(self._dir, exist_ok=True)
            path = self._path(agent_id)
            plaintext = json.dumps(record).encode("utf-8")
            blob = _enc.encrypt_blob(
                self._dek, plaintext, aad=agent_id.encode("utf-8")
            )
            tmp = f"{path}.{os.getpid()}.tmp"
            with open(tmp, "wb") as f:
                f.write(blob)
            os.replace(tmp, path)
            return True
        except (OSError, TypeError, ValueError):
            return False

    def load(self, agent_id: str) -> dict | None:
        """Decrypt and return a single definition, or None if missing/unreadable."""
        try:
            with open(self._path(agent_id), "rb") as f:
                blob = f.read()
            plaintext = _enc.decrypt_blob(
                self._dek, blob, aad=agent_id.encode("utf-8")
            )
            return json.loads(plaintext.decode("utf-8"))
        except (OSError, ValueError, InvalidTag):
            # Missing, corrupt, or AAD-mismatched (wrong id) — treat as unreadable.
            return None

    def list_agents(self) -> list[dict]:
        """Every saved definition for this user, newest first. Definitions are
        small, so the full record is returned (no metadata projection needed).
        Unreadable files are skipped rather than failing the listing."""
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

    def delete(self, agent_id: str) -> bool:
        """Remove a saved definition. Returns True if a file was deleted, False
        if it didn't exist or couldn't be removed. Run records the agent already
        produced are left untouched — they're a separate, immutable audit trail."""
        try:
            os.remove(self._path(agent_id))
            return True
        except OSError:
            return False
