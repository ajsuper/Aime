"""Application-wide configuration: paths, model id, schema list, system prompt.

These were originally constants at the top of `tui_model.py`. They are kept in
the core layer so non-TUI frontends (and library callers) can share them.
TUI-only settings (e.g. theme prefs) deliberately stay in `tui_model.py`.
"""

import os


API_URL = "http://localhost:8080/api"

# Where Aime keeps per-user data. Mirrors the C++ backend's database_dir.
# Auth state (auth.sql, secret_key) lives at the root; per-user databases
# will live under users/<id>/ once the C++ side learns to route by user_id.
DATABASE_DIR = os.environ.get(
    "AIME_DATABASE_DIR",
    os.path.join(os.environ["HOME"], ".local/share/aime-assistant/database"),
)

# Agent configuration file used by the Sessions backend to cache its
# server-side environment/agent setup. The Messages backend ignores it.
CONFIG_PATH = os.environ['HOME'] + "/.config/aime-assistant/agents_config.json"

# System prompt path is resolved relative to src/ — the same working directory
# Aime is launched from.
SYSTEM_PROMPT_PATH = "../resources/prompts/system_prompt.md"

SCHEMA_FILES = [
    "../resources/tools/api_request_schema.json",
    "../resources/tools/api_replace_event_schema.json",
    "../resources/tools/api_create_event_schema.json",
    "../resources/tools/api_request_topics_schema.json",
    "../resources/tools/api_create_topic_schema.json",
    "../resources/tools/api_replace_topic_schema.json",
    "../resources/tools/api_get_topic_contents_schema.json",
    "../resources/tools/api_replace_topic_contents_schema.json",
    "../resources/tools/api_edit_topic_contents_schema.json",
    "../resources/tools/api_rename_folder_schema.json",
    "../resources/tools/api_list_folders_schema.json",
    "../resources/tools/api_get_commitment_history_schema.json",
    "../resources/tools/api_get_pattern_summary_schema.json",
    "../resources/tools/api_get_recent_activity_schema.json",
]

AGENT_MODEL = "claude-sonnet-4-6"

# Model routing: cheap Haiku for read-only lookup turns, Sonnet for anything
# that creates/edits state or needs multi-step reasoning. A small classifier
# Haiku call picks per turn; see aime.model_router.
SONNET_MODEL = "claude-sonnet-4-6"
HAIKU_MODEL = "claude-haiku-4-5-20251001"
ROUTER_MODEL = "claude-haiku-4-5-20251001"

# Web search is offloaded to a Haiku sub-agent (see aime.web_search_agent): the
# conversational model calls a single `WebSearch` tool, and Haiku runs the
# native server-side search, reads the raw results, and returns a compact
# summary + sources. Keeps bulky search results out of the expensive model's
# (re-cached every turn) context. WEB_SEARCH_MODEL is the model that runs the
# search; WEB_SEARCH_TOOL_VERSION is the native tool it uses (the 20250305
# version is GA on Haiku — dynamic filtering isn't needed, Haiku's read+
# summarize IS the filtering).
WEB_SEARCH_SCHEMA = "../resources/tools/api_web_search_schema.json"
WEB_SEARCH_MODEL = HAIKU_MODEL
WEB_SEARCH_TOOL_VERSION = "web_search_20250305"

# Tool offered to the model only during first-time onboarding, so it can mark
# the flow finished once it delivers its closing message. Presentation is gated
# per-turn by the backend (see set_terminal_tool_active); the schema just
# loads here like any other. It is the "terminal tool" for the interactive
# backend — see the background-agent framework for the other use of that slot.
ONBOARDING_TOOL_SCHEMA = "../resources/tools/api_complete_onboarding_schema.json"

# --- Background agents (aime.agents) -----------------------------------------
# Headless workers that reuse the full Aime stack (backend + controller + tool
# gateway) to carry out a task against a user's database and return a result.
# The base system prompt frames the model as a headless worker; the per-agent
# task arrives via system_send_message. SubmitResult is the terminal tool the
# worker calls to deliver its result and end the run.
AGENT_BASE_PROMPT_PATH = "../resources/prompts/agents/_base.md"
SUBMIT_RESULT_SCHEMA = "../resources/tools/api_submit_result_schema.json"
# Per-user directory (sibling of conversations/, under users/<id>/) where
# background-agent run records are persisted, encrypted with the user's DEK.
AGENT_RUNS_DIRNAME = "agent_runs"


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return bool(int(raw))
    except ValueError:
        return raw.strip().lower() in ("1", "true", "yes", "on")


# AIME_MODEL_ROUTING=0 forces every turn to Sonnet (the original behavior).
MODEL_ROUTING_ENABLED = _env_flag("AIME_MODEL_ROUTING", True)

# AIME_WEB_SEARCH=0 drops the WebSearch tool entirely (no web access).
WEB_SEARCH_ENABLED = _env_flag("AIME_WEB_SEARCH", True)


def load_system_prompt(path: str = SYSTEM_PROMPT_PATH) -> str:
    with open(path) as f:
        return f.read()


def load_agent_base_prompt(path: str = AGENT_BASE_PROMPT_PATH) -> str:
    """The headless background-worker system prompt (see aime.agents)."""
    with open(path) as f:
        return f.read()
