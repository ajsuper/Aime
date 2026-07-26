"""SSE framing must be total: no event can ever kill a client's stream.

The /stream generator used to encode with a bare `json.dumps`. One value it
couldn't handle raised *inside the generator*, dropping that connection. The
client treats a dropped stream as a reconnect, and a reconnect wipes the
transcript before replaying it — so the browser cleared the chat, replayed up
to the same bad event, died again, and looped. A blank chat that refreshing
could not fix, because the offending event lived in the in-memory replay cache;
only a process restart (which rebuilds it from the durable messages) cleared it.
"""

import datetime
import json

import frontends.web_app as web_app


def _decode(frame):
    assert frame.startswith("data: ") and frame.endswith("\n\n")
    return json.loads(frame[len("data: "):-2])


def test_plain_payload_round_trips():
    frame = web_app._sse_frame({"kind": "assistant_html", "text": "<p>hi</p>"})
    assert _decode(frame) == {"kind": "assistant_html", "text": "<p>hi</p>"}


def test_unserializable_value_is_coerced_not_raised():
    payload = {
        "kind": "tool_result",
        "payload": {"when": datetime.datetime(2026, 7, 26, 9, 30)},
    }
    got = _decode(web_app._sse_frame(payload))
    assert got["kind"] == "tool_result"
    # Coerced to *something* JSON-safe rather than taking the stream down.
    assert isinstance(got["payload"]["when"], str)


def test_sse_headers_are_wsgi_legal():
    """No hop-by-hop headers on the /stream response.

    PEP 3333 forbids a WSGI app from setting them and waitress enforces it with
    an AssertionError — so a `Connection: keep-alive` copied from the usual SSE
    recipe doesn't degrade the stream, it 500s the whole response. Every
    EventSource then fails, retries, fails again: a permanent "Connection lost
    — reconnecting…" with no chat at all.
    """
    hop_by_hop = {
        "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
        "te", "trailers", "transfer-encoding", "upgrade",
    }
    offenders = {h for h in web_app._SSE_HEADERS if h.lower() in hop_by_hop}
    assert not offenders, f"hop-by-hop header(s) on /stream: {offenders}"


def test_sse_headers_disable_proxy_buffering():
    # The reason these headers exist at all: nginx buffers upstream responses by
    # default, which silently swallows an SSE stream until the buffer flushes.
    assert web_app._SSE_HEADERS["X-Accel-Buffering"] == "no"
    assert "no-transform" in web_app._SSE_HEADERS["Cache-Control"]


def test_unencodable_payload_degrades_to_a_harmless_event():
    circular = {}
    circular["self"] = circular
    frame = web_app._sse_frame({"kind": "graphic", "payload": circular})
    # Still a well-formed frame the client can parse and ignore.
    assert _decode(frame)["kind"] == "notice"
