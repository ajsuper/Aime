"""Aime core — UI-agnostic conversation logic.

Layers:
    provider_backend.AgentBackend   provider/model I/O
    aime.*                          conversation logic (this package)
    tui_model / other frontend      presentation

Frontends construct an `AgentBackend`, a `ToolGateway`, and a
`ConversationController`, subscribe to the controller's `CoreEvent` stream,
and forward user input via `controller.dispatch_input(...)`.

Importing the package is deliberately cheap: the conversation modules (and
the Anthropic SDK they pull in via provider_backend) are loaded lazily, only
when one of the names below is first accessed. This lets lightweight tooling
— e.g. scripts/access_keys.py — do `from aime import auth, config` without
dragging in the model backend or requiring `anthropic` to be installed.
"""

# Map each lazily-exported name to the submodule that defines it.
_LAZY_EXPORTS = {
    "ConversationController": ".controller",
    "CoreEvent": ".controller",
    "CoreEventKind": ".controller",
    "ToolGateway": ".tool_gateway",
    "CalendarService": ".services",
    "TopicService": ".services",
}

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


def __getattr__(name):
    """PEP 562 lazy attribute access. `config` and `auth` are lightweight
    submodules and resolve via the normal import machinery; the rest are
    loaded on first use so a bare `import aime` stays free of heavy deps."""
    import importlib

    if name in _LAZY_EXPORTS:
        module = importlib.import_module(_LAZY_EXPORTS[name], __name__)
        return getattr(module, name)
    if name in ("config", "auth"):
        return importlib.import_module(f".{name}", __name__)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted(__all__)
