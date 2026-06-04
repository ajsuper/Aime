"""Regression tests for shared-topic access guarding in the controller.

A shared topic is addressed by a composite "<owner>:<topic>" handle. Only the
contents-level tools may touch it (and only with the right grant); a metadata
edit such as ReplaceTopic must be refused cleanly. Critically, the composite
handle must never fall through to the user's *own* gateway, where it would be
truncated at the ":" and silently edit an unrelated local topic — the bug this
suite locks down.
"""

import sys

import pytest

from provider_backend import BackendEvent
from aime.controller import ConversationController


class _FakeBackend:
    conversations_dir = None

    def __init__(self):
        self.responses = []

    def submit(self, event: BackendEvent):
        if event.kind == "tool_send_response":
            self.responses.append(event)


class _FakeGateway:
    """Stands in for the user's *own* data backend. Records every call so a
    test can assert a composite handle never reached it."""

    def __init__(self):
        self.calls = []

    def execute(self, tool_name, tool_input):
        self.calls.append((tool_name, dict(tool_input or {})))
        return {"ok": True}


class _FakeRecordSync:
    def __init__(self):
        self.shared_calls = []

    def run_if_shared(self, tool_name, tool_input):
        self.shared_calls.append((tool_name, dict(tool_input or {})))
        # Pretend the grant check passed and the owner's gateway ran it.
        return {"ok": True, "routed": True}

    def merge_shared_into_list(self, result, tool_input=None):
        return result

    def after_model_write(self, tool_name, tool_input, result):
        pass


def _make_controller():
    backend = _FakeBackend()
    gateway = _FakeGateway()
    record_sync = _FakeRecordSync()
    controller = ConversationController(
        backend=backend,
        tool_gateway=gateway,
        worker_spawner=lambda fn: None,
        record_sync=record_sync,
    )
    return controller, backend, gateway, record_sync


def _fire(controller, tool_name, tool_input):
    controller._handle_tool_use(BackendEvent(
        kind="tool_use",
        tool_name=tool_name,
        tool_input=tool_input,
        tool_use_id="t1",
    ))


def test_replace_topic_on_shared_handle_is_denied_not_truncated():
    controller, backend, gateway, record_sync = _make_controller()

    _fire(controller, "ReplaceTopic", {"id": "2:35", "title": "hijacked"})

    # The composite handle must NOT have reached the user's own gateway (which
    # would have edited local topic 2), nor been routed as a content edit.
    assert gateway.calls == []
    assert record_sync.shared_calls == []
    # The model gets a clean permission-denied result it can relay.
    assert len(backend.responses) == 1
    err = backend.responses[0].tool_result
    assert isinstance(err, dict) and "error" in err
    assert "contents" in err["error"].lower()


def test_replace_topic_on_own_bare_id_still_runs_locally():
    controller, backend, gateway, record_sync = _make_controller()

    _fire(controller, "ReplaceTopic", {"id": 7, "title": "fine"})

    assert gateway.calls == [("ReplaceTopic", {"id": 7, "title": "fine"})]
    assert record_sync.shared_calls == []


def test_content_edit_on_shared_handle_is_routed_through_bridge():
    controller, backend, gateway, record_sync = _make_controller()

    _fire(controller, "EditTopicContents",
          {"id": "2:35", "old_string": "a", "new_string": "b"})

    # Routed to the share bridge; never the local gateway.
    assert gateway.calls == []
    assert len(record_sync.shared_calls) == 1
    assert record_sync.shared_calls[0][0] == "EditTopicContents"


def test_content_edit_on_bare_id_normalizes_and_runs():
    controller, backend, gateway, record_sync = _make_controller()

    # A bare numeric-string id is coerced to int and run via the bridge's
    # bare-id passthrough (run_if_shared returns the result here).
    _fire(controller, "GetTopicContents", {"id": "9"})

    assert record_sync.shared_calls == [("GetTopicContents", {"id": 9})]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
