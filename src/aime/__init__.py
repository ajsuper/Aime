"""Aime core — UI-agnostic conversation logic.

Layers:
    provider_backend.AgentBackend   provider/model I/O
    aime.*                          conversation logic (this package)
    tui_model / other frontend      presentation

Frontends construct an `AgentBackend`, a `ToolGateway`, and a
`ConversationController`, subscribe to the controller's `CoreEvent` stream,
and forward user input via `controller.dispatch_input(...)`.
"""

from .controller import ConversationController, CoreEvent, CoreEventKind
from .tool_gateway import ToolGateway
from .services import CalendarService, TopicService
from . import config
from . import auth

__all__ = [
    "ConversationController",
    "CoreEvent",
    "CoreEventKind",
    "ToolGateway",
    "CalendarService",
    "TopicService",
    "config",
    "auth",
]
