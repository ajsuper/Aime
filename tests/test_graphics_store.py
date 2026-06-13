"""Unit tests for aime.graphics_store — the per-user graphic asset store.

Covers the round-trip (create → list → load → delete), monotonic id allocation
that survives gaps and ignores stray files, that the on-disk blob is opaque and
AAD-bound to the asset id, and that malformed records never reach disk.
"""

import pytest

from aime import encryption as enc
from aime import graphics_store as gs


@pytest.fixture
def dek():
    return enc.generate_dek()


@pytest.fixture
def store(tmp_path, dek):
    return gs.GraphicStore(str(tmp_path / "graphics"), dek)


def test_create_allocates_first_id_and_round_trips(store):
    rec = store.create("mermaid", "flowchart TD\nA-->B", "A to B")
    assert rec["id"] == "graphic-1"
    assert rec["format"] == "mermaid"
    assert rec["source"] == "flowchart TD\nA-->B"
    assert rec["summary"] == "A to B"
    assert rec["created_at"] and rec["updated_at"]

    loaded = store.load("graphic-1")
    assert loaded == rec


def test_ids_are_monotonic_across_creates(store):
    a = store.create("mermaid", "flowchart TD\nA-->B", "")
    b = store.create("svg", '<svg viewBox="0 0 1 1"></svg>', "")
    c = store.create("mermaid", "graph TD\nX-->Y", "")
    assert [a["id"], b["id"], c["id"]] == ["graphic-1", "graphic-2", "graphic-3"]


def test_next_id_is_one_past_highest_even_with_gaps(store):
    store.create("mermaid", "flowchart TD\nA-->B", "")  # graphic-1
    store.create("mermaid", "graph TD\nX-->Y", "")       # graphic-2
    assert store.delete("graphic-1") is True
    # Highest on disk is now graphic-2, so the next id is graphic-3 (ids never
    # get reused even after a delete).
    rec = store.create("mermaid", "graph TD\nP-->Q", "")
    assert rec["id"] == "graphic-3"


def test_stray_files_are_ignored_for_allocation(tmp_path, store):
    store.create("mermaid", "flowchart TD\nA-->B", "")  # graphic-1
    # A non-matching file in the dir must not perturb the high-water mark.
    (tmp_path / "graphics" / "notes.txt").write_text("ignore me")
    rec = store.create("mermaid", "graph TD\nX-->Y", "")
    assert rec["id"] == "graphic-2"


def test_list_returns_newest_first(store):
    store.create("mermaid", "flowchart TD\nA-->B", "first")
    store.create("mermaid", "graph TD\nX-->Y", "second")
    listed = store.list_graphics()
    assert [r["summary"] for r in listed] == ["second", "first"]


def test_load_missing_returns_none(store):
    assert store.load("graphic-99") is None


def test_load_rejects_non_id(store):
    assert store.load("not-an-id") is None
    assert store.load("graphic-") is None


def test_delete_missing_returns_false(store):
    assert store.delete("graphic-99") is False


def test_save_rejects_bad_format(store):
    bad = {"id": "graphic-1", "format": "png", "source": "x", "summary": ""}
    assert store.save(bad) is False
    assert store.load("graphic-1") is None


def test_save_rejects_missing_id(store):
    assert store.save({"format": "mermaid", "source": "x", "summary": ""}) is False


def test_blob_is_opaque_and_aad_bound(tmp_path, dek, store):
    rec = store.create("mermaid", "flowchart TD\nSECRET-->NODE", "")
    path = tmp_path / "graphics" / "graphic-1.json.enc"
    blob = path.read_bytes()
    assert b"SECRET" not in blob  # encrypted at rest

    # Renaming the file to a different id breaks the AAD bind: the store reads it
    # as unreadable rather than silently returning another asset's plaintext.
    path.rename(tmp_path / "graphics" / "graphic-2.json.enc")
    assert store.load("graphic-2") is None


def test_id_ordinal_helpers():
    assert gs.make_graphic_id(7) == "graphic-7"
    assert gs.graphic_id_ordinal("graphic-7") == 7
    assert gs.graphic_id_ordinal("fig-7") is None
    assert gs.graphic_id_ordinal("graphic-") is None
    assert gs.graphic_id_ordinal(None) is None
