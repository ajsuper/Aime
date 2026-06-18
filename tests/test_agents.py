"""Tests for the background-agent framework (``aime.agents``).

Background agents run the full tool stack autonomously, so the property that
matters most here is **least privilege**: an agent may only do what its
``tool_allowlist`` grants, and there must be no way to reach a capability the
allowlist withholds. The highest-value assertions below are the ones that lock
that down — particularly that an agent without the "send message" permission is
offered *no* path to message the user, including the SubmitResult
``message_to_user`` field.

These tests are hermetic: they don't touch the model API and they point
``config`` at synthetic schema files so they don't depend on the process's
working directory or the real resource tree.
"""

import json

import pytest

from aime import config
from aime.agents import spec as spec_mod
from aime.agents.spec import (
    AgentSpec,
    AgentResult,
    permissions_to_allowlist,
    READONLY_TOOLS,
    MODIFY_EVENTS_TOOLS,
    MODIFY_TOPICS_TOOLS,
    SEND_MESSAGE_TOOLS,
    WEB_SEARCH_TOOLS,
)
from aime.agents import registry
from aime.agents.runner import BackgroundAgentRunner


# --------------------------------------------------------------------------- #
# permissions_to_allowlist — least-privilege gate construction
# --------------------------------------------------------------------------- #
def test_no_permissions_is_readonly_baseline_only():
    allow = permissions_to_allowlist()
    assert allow == READONLY_TOOLS
    # None of the mutating/side-effecting groups leak in.
    for locked in (MODIFY_EVENTS_TOOLS, MODIFY_TOPICS_TOOLS,
                   SEND_MESSAGE_TOOLS, WEB_SEARCH_TOOLS):
        assert not (allow & locked)


def test_each_toggle_unlocks_exactly_its_group():
    assert permissions_to_allowlist(modify_events=True) == READONLY_TOOLS | MODIFY_EVENTS_TOOLS
    assert permissions_to_allowlist(modify_topics=True) == READONLY_TOOLS | MODIFY_TOPICS_TOOLS
    assert permissions_to_allowlist(send_message=True) == READONLY_TOOLS | SEND_MESSAGE_TOOLS
    assert permissions_to_allowlist(web_search=True) == READONLY_TOOLS | WEB_SEARCH_TOOLS


def test_all_toggles_compose():
    allow = permissions_to_allowlist(
        modify_events=True, modify_topics=True, send_message=True, web_search=True,
    )
    assert allow == (READONLY_TOOLS | MODIFY_EVENTS_TOOLS | MODIFY_TOPICS_TOOLS
                     | SEND_MESSAGE_TOOLS | WEB_SEARCH_TOOLS)


# --------------------------------------------------------------------------- #
# Capability properties — None allowlist means "full toolset"
# --------------------------------------------------------------------------- #
def _spec(**kw) -> AgentSpec:
    base = dict(name="t", description="d", instructions="do it")
    base.update(kw)
    return AgentSpec(**base)


def test_web_search_allowed_logic():
    assert _spec(tool_allowlist=None).web_search_allowed is True
    assert _spec(tool_allowlist=frozenset({"WebSearch"})).web_search_allowed is True
    assert _spec(tool_allowlist=frozenset({"FilterTopics"})).web_search_allowed is False


def test_messaging_allowed_logic():
    assert _spec(tool_allowlist=None).messaging_allowed is True
    assert _spec(tool_allowlist=frozenset({"SendMessage"})).messaging_allowed is True
    assert _spec(tool_allowlist=frozenset({"FilterTopics"})).messaging_allowed is False


# --------------------------------------------------------------------------- #
# render_kickoff — template filling fails loud, not silent
# --------------------------------------------------------------------------- #
def test_render_kickoff_wraps_and_fills_placeholders():
    out = _spec(instructions="Audit {month}").render_kickoff({"month": "June"})
    assert "Audit June" in out
    assert out.startswith("[system: background task]")


def test_render_kickoff_missing_placeholder_raises():
    # A non-empty inputs dict triggers .format(); a brief that references a key
    # the inputs don't supply fails loudly rather than emitting a broken brief.
    with pytest.raises(KeyError):
        _spec(instructions="Audit {month}").render_kickoff({"other": "x"})


def test_render_kickoff_without_inputs_is_literal():
    # No inputs => no .format(), so stray braces in a static brief don't blow up.
    out = _spec(instructions="plain brief").render_kickoff()
    assert out.endswith("plain brief")


# --------------------------------------------------------------------------- #
# submit_result_raw_schema — the privilege guard on the terminal tool
# --------------------------------------------------------------------------- #
@pytest.fixture
def synthetic_submit_schema(tmp_path, monkeypatch):
    """Point config.SUBMIT_RESULT_SCHEMA at a known schema with a
    message_to_user field, so the drop/keep logic can be asserted hermetically."""
    schema = {
        "title": "SubmitResult",
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "result": {"type": "object"},
            "message_to_user": {"type": "string"},
        },
    }
    p = tmp_path / "submit_result.json"
    p.write_text(json.dumps(schema))
    monkeypatch.setattr(config, "SUBMIT_RESULT_SCHEMA", str(p))
    return p


def test_message_to_user_dropped_when_messaging_not_allowed(synthetic_submit_schema):
    # An agent without SendMessage must be offered NO path to the user — the
    # terminal tool is always armed, so the field has to be removed.
    spec = _spec(tool_allowlist=frozenset({"FilterTopics"}))
    props = spec.submit_result_raw_schema()["properties"]
    assert "message_to_user" not in props
    assert "summary" in props  # the rest of the schema is intact


def test_message_to_user_kept_when_messaging_allowed(synthetic_submit_schema):
    spec = _spec(tool_allowlist=frozenset({"SendMessage"}))
    assert "message_to_user" in spec.submit_result_raw_schema()["properties"]
    # And the full-toolset (None) case keeps it too.
    assert "message_to_user" in _spec().submit_result_raw_schema()["properties"]


def test_result_schema_specializes_the_result_property(synthetic_submit_schema):
    custom = {"type": "object", "properties": {"score": {"type": "number"}}}
    spec = _spec(result_schema=custom)
    assert spec.submit_result_raw_schema()["properties"]["result"] == custom


# --------------------------------------------------------------------------- #
# _schema_files_for — the data-tool allowlist filter
# --------------------------------------------------------------------------- #
@pytest.fixture
def synthetic_schema_files(tmp_path, monkeypatch):
    """Three fake tool-schema files with known titles, installed as
    config.SCHEMA_FILES (plus one unreadable path to exercise the skip)."""
    titles = ["FilterTopics", "CreateEvent", "GetTopicContents"]
    paths = []
    for t in titles:
        p = tmp_path / f"{t}.json"
        p.write_text(json.dumps({"title": t}))
        paths.append(str(p))
    paths.append(str(tmp_path / "does_not_exist.json"))  # skipped silently
    monkeypatch.setattr(config, "SCHEMA_FILES", paths)
    return paths


def test_schema_files_none_allowlist_returns_full_set(synthetic_schema_files):
    out = BackgroundAgentRunner._schema_files_for(_spec(tool_allowlist=None))
    assert out is config.SCHEMA_FILES


def test_schema_files_filters_to_allowlisted_titles(synthetic_schema_files):
    spec = _spec(tool_allowlist=frozenset({"FilterTopics", "GetTopicContents"}))
    out = BackgroundAgentRunner._schema_files_for(spec)
    kept = {json.load(open(p))["title"] for p in out}
    assert kept == {"FilterTopics", "GetTopicContents"}
    # The non-allowlisted tool and the unreadable path are both absent.
    assert all("CreateEvent" not in p and "does_not_exist" not in p for p in out)


def test_schema_files_empty_allowlist_keeps_nothing(synthetic_schema_files):
    out = BackgroundAgentRunner._schema_files_for(_spec(tool_allowlist=frozenset()))
    assert out == []


# --------------------------------------------------------------------------- #
# registry — dispatch by name
# --------------------------------------------------------------------------- #
@pytest.fixture
def clean_registry(monkeypatch):
    """Isolate the module-global registry so tests don't pollute each other."""
    monkeypatch.setattr(registry, "_REGISTRY", {})
    return registry


def test_register_and_get_roundtrip(clean_registry):
    spec = _spec(name="auditor")
    assert clean_registry.register(spec) is spec  # returns the spec for one-liners
    assert clean_registry.get("auditor") is spec


def test_register_empty_name_raises(clean_registry):
    with pytest.raises(ValueError):
        clean_registry.register(_spec(name=""))


def test_register_replaces_by_name(clean_registry):
    clean_registry.register(_spec(name="dup", description="first"))
    clean_registry.register(_spec(name="dup", description="second"))
    assert clean_registry.get("dup").description == "second"


def test_get_unknown_lists_known_names(clean_registry):
    clean_registry.register(_spec(name="alpha"))
    with pytest.raises(KeyError) as exc:
        clean_registry.get("missing")
    assert "alpha" in str(exc.value)  # error names what *is* registered


def test_all_specs_sorted_by_name(clean_registry):
    clean_registry.register(_spec(name="zeta"))
    clean_registry.register(_spec(name="alpha"))
    assert [s.name for s in clean_registry.all_specs()] == ["alpha", "zeta"]


# --------------------------------------------------------------------------- #
# AgentResult — ok only on a clean completion
# --------------------------------------------------------------------------- #
def test_agent_result_ok_only_when_completed():
    assert AgentResult(status="completed", summary_text="s").ok is True
    for bad in ("max_turns", "no_result", "error"):
        assert AgentResult(status=bad, summary_text="s").ok is False
