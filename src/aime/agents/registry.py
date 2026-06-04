"""A tiny name -> AgentSpec registry.

Background agents register themselves here so callers (a scheduler, a web
endpoint, or the main agent) can dispatch one by name without importing its
module directly. The framework ships empty — concrete agents register their
spec at import time:

    from aime.agents import register, AgentSpec
    register(AgentSpec(name="calendar-auditor", ...))

Lookup is a plain dict; registration is idempotent-by-replacement so reloading
a module during development doesn't raise.
"""

from .spec import AgentSpec


_REGISTRY: dict[str, AgentSpec] = {}


def register(spec: AgentSpec) -> AgentSpec:
    """Register (or replace) an agent by its ``name``. Returns the spec so it
    can be used as a decorator-style one-liner at module scope."""
    if not spec.name:
        raise ValueError("AgentSpec.name must be a non-empty string")
    _REGISTRY[spec.name] = spec
    return spec


def get(name: str) -> AgentSpec:
    """Look up a registered agent by name. Raises KeyError with the known names
    if it isn't registered, so a typo'd dispatch fails clearly."""
    try:
        return _REGISTRY[name]
    except KeyError:
        known = ", ".join(sorted(_REGISTRY)) or "(none registered)"
        raise KeyError(f"no background agent named {name!r}; known: {known}")


def all_specs() -> list[AgentSpec]:
    """Every registered spec, sorted by name. For listing UIs / introspection."""
    return [_REGISTRY[n] for n in sorted(_REGISTRY)]
