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


def test_format_graphic_id():
    assert gs.format_graphic_id("0", 3) == "graphic-0:3"
    assert gs.format_graphic_id("5", 3) == "graphic-5:3"
    assert gs.format_graphic_id("4:5", 3) == "graphic-4:5:3"


def test_parse_graphic_id_full_forms():
    assert gs.parse_graphic_id("graphic-0:3") == ("0", 3)
    assert gs.parse_graphic_id("graphic-5:3") == ("5", 3)
    assert gs.parse_graphic_id("graphic-4:5:3") == ("4:5", 3)


def test_parse_graphic_id_legacy_bare_is_personal():
    # A legacy bare graphic-N (no colon) reads as personal graphic-0:N.
    assert gs.parse_graphic_id("graphic-7") == ("0", 7)


def test_id_handle_is_absolute_for_topics_and_zero_for_personal(tmp_path, dek):
    base = str(tmp_path / "graphics")
    assert gs.GraphicStore(base, dek, owner_id=1, topic_id=0).id_handle == "0"
    assert gs.GraphicStore(base, dek, owner_id=1, topic_id=7).id_handle == "1:7"
    assert gs.GraphicStore(base, dek, owner_id=4, topic_id=5).id_handle == "4:5"


def test_tag_handle_scope():
    # A bare topic handle belongs to the topic's owner (passed in).
    assert gs.tag_handle_scope("5", 4) == (4, 5)
    # An explicit O:T names its own owner, regardless of the topic owner arg.
    assert gs.tag_handle_scope("4:5", 4) == (4, 5)
    assert gs.tag_handle_scope("3:9", 4) == (3, 9)
    # Personal (and legacy bare, which collapses to "0") is never a topic graphic.
    assert gs.tag_handle_scope("0", 4) is None
    # Garbage handles resolve to nothing rather than raising.
    assert gs.tag_handle_scope("a", 4) is None
    assert gs.tag_handle_scope("1:2:3", 4) is None


def test_parse_graphic_id_rejects_garbage():
    assert gs.parse_graphic_id("fig-1:2") is None
    assert gs.parse_graphic_id("graphic-") is None
    assert gs.parse_graphic_id("graphic-a:1") is None
    assert gs.parse_graphic_id("graphic-1:b") is None
    assert gs.parse_graphic_id("graphic-1:2:3:4") is None
    assert gs.parse_graphic_id(None) is None


def test_topic_scoped_store_nests_in_subdir(tmp_path, dek):
    base = str(tmp_path / "graphics")
    store = gs.GraphicStore(base, dek, owner_id=10, topic_id=5)
    rec = store.create("mermaid", "flowchart TD\nA-->B", "in topic")
    assert rec["id"] == "graphic-1"
    # Lives under topic-5/, not the personal base dir.
    assert (tmp_path / "graphics" / "topic-5" / "graphic-1.json.enc").exists()
    assert not (tmp_path / "graphics" / "graphic-1.json.enc").exists()
    assert store.load("graphic-1") == rec


def test_personal_and_topic_stores_allocate_independently(tmp_path, dek):
    base = str(tmp_path / "graphics")
    personal = gs.GraphicStore(base, dek, owner_id=10, topic_id=0)
    topic = gs.GraphicStore(base, dek, owner_id=10, topic_id=5)
    a = personal.create("mermaid", "graph TD\nA-->B", "")
    b = topic.create("mermaid", "graph TD\nX-->Y", "")
    # Each scope has its own ordinal sequence starting at 1.
    assert a["id"] == "graphic-1"
    assert b["id"] == "graphic-1"


def test_create_rejects_bad_format(store):
    assert store.create("png", "x", "") is None


def test_concurrent_creates_get_distinct_ids(tmp_path, dek):
    # Two stores over the same scope (separate handles, as different writers /
    # processes would have) must never collide on an ordinal.
    base = str(tmp_path / "graphics")
    s1 = gs.GraphicStore(base, dek, owner_id=10, topic_id=5)
    s2 = gs.GraphicStore(base, dek, owner_id=10, topic_id=5)
    ids = set()
    for i in range(20):
        store = s1 if i % 2 == 0 else s2
        rec = store.create("mermaid", f"graph TD\nA{i}-->B", "")
        ids.add(rec["id"])
    assert len(ids) == 20
