"""Flask frontend for Aime — minimal chat only.

Mirrors `tui_model.py` at the wiring layer: builds an `AgentBackend`, a
`ToolGateway`, and a `ConversationController`, then renders the controller's
`CoreEvent` stream — here, over Server-Sent Events to a single HTML page.

Run from the project's `src/` directory:

    python -m frontends.web_app

then open http://127.0.0.1:5000/.
"""

import os
import sys
import json
import queue
import threading
from io import StringIO

from flask import Flask, Response, request, jsonify
from rich.console import Console
from rich.text import Text

# Allow `python -m frontends.web_app` from src/ to find provider_backend / aime.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from provider_backend import AnthropicMessagesBackend

from aime import (
    ConversationController,
    CoreEvent,
    ToolGateway,
    config as aime_config,
)


# ---------------------------------------------------------------------------
# Controller wiring
# ---------------------------------------------------------------------------

def _build_controller() -> ConversationController:
    backend = AnthropicMessagesBackend(
        system_prompt=aime_config.load_system_prompt(),
        model=aime_config.AGENT_MODEL,
        schema_files=aime_config.SCHEMA_FILES,
    )
    backend.new_session()

    gateway = ToolGateway(api_url=aime_config.API_URL)

    def spawn_worker(fn):
        threading.Thread(target=fn, name="agent-stream", daemon=True).start()

    return ConversationController(
        backend=backend,
        tool_gateway=gateway,
        worker_spawner=spawn_worker,
    )


# A single shared controller per process. Subscribers are called from whichever
# thread emits the event; we shove each one into a thread-safe queue per SSE
# client.
_controller = _build_controller()
_subscribers_lock = threading.Lock()
_client_queues: list[queue.Queue] = []

# Buffer of streamed assistant text deltas for the in-progress block. Rich
# markup tags can straddle delta boundaries, so we render to HTML only once
# the block ends.
_assistant_buf: list[str] = []
_assistant_buf_lock = threading.Lock()


def _render_markup_to_html(markup: str) -> str:
    """Convert Rich-style markup to inline-styled HTML spans."""
    console = Console(
        record=True,
        file=StringIO(),
        force_terminal=True,
        color_system="truecolor",
        width=10_000,
    )
    try:
        rendered = Text.from_markup(markup)
    except Exception:
        # Malformed markup — fall back to plain text so the user still sees it.
        rendered = Text(markup)
    console.print(rendered, soft_wrap=True, end="")
    return console.export_html(inline_styles=True, code_format="{code}")


def _broadcast(payload: dict) -> None:
    with _subscribers_lock:
        targets = list(_client_queues)
    for q in targets:
        try:
            q.put_nowait(payload)
        except queue.Full:
            pass


def _fanout(event: CoreEvent) -> None:
    # Track streaming text so we can re-render the completed block as HTML.
    if event.kind == "assistant_text_delta":
        with _assistant_buf_lock:
            _assistant_buf.append(event.text or "")
    elif event.kind in ("user_message_shown", "session_restart"):
        with _assistant_buf_lock:
            _assistant_buf.clear()

    payload = {
        "kind": event.kind,
        "text": event.text,
        "tool_name": event.tool_name,
        "tool_details": event.tool_details,
        "tool_result_summary": event.tool_result_summary,
        "severity": event.severity,
        "stop_reason": event.stop_reason,
        "from_replay": event.from_replay,
    }
    _broadcast(payload)

    # After delivering the raw event, emit a rendered-HTML companion so the
    # client can swap the live-streamed plain text for styled output.
    if event.kind == "assistant_text_end":
        with _assistant_buf_lock:
            full = "".join(_assistant_buf)
            _assistant_buf.clear()
        if full:
            _broadcast({"kind": "assistant_html", "text": _render_markup_to_html(full)})
    elif event.kind == "assistant_text" and event.text:
        _broadcast({"kind": "assistant_html", "text": _render_markup_to_html(event.text)})


_controller.subscribe(_fanout)
_controller.start()


# ---------------------------------------------------------------------------
# HTTP app
# ---------------------------------------------------------------------------

app = Flask(__name__)


_PAGE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "resources", "style", "web_chat.html",
)


def _load_page() -> str:
    with open(_PAGE_PATH) as f:
        return f.read()


@app.route("/")
def index():
    return Response(_load_page(), mimetype="text/html")


@app.route("/send", methods=["POST"])
def send():
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"ok": False, "error": "empty"}), 400
    should_quit = _controller.dispatch_input(text)
    return jsonify({"ok": True, "quit": should_quit})


@app.route("/stream")
def stream():
    q: queue.Queue = queue.Queue(maxsize=1024)
    with _subscribers_lock:
        _client_queues.append(q)

    def gen():
        try:
            while True:
                payload = q.get()
                yield f"data: {json.dumps(payload)}\n\n"
        finally:
            with _subscribers_lock:
                if q in _client_queues:
                    _client_queues.remove(q)

    return Response(gen(), mimetype="text/event-stream")


if __name__ == "__main__":
    # threaded=True so the SSE generator and /send can run concurrently.
    app.run(host="127.0.0.1", port=5000, threaded=True, debug=False)
