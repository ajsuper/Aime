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
]

AGENT_MODEL = "claude-sonnet-4-6"

# Model routing: cheap Haiku for read-only lookup turns, Sonnet for anything
# that creates/edits state or needs multi-step reasoning. A small classifier
# Haiku call picks per turn; see aime.model_router.
SONNET_MODEL = "claude-sonnet-4-6"
HAIKU_MODEL = "claude-haiku-4-5-20251001"
ROUTER_MODEL = "claude-haiku-4-5-20251001"


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
# AIME_VERBOSE=1 surfaces per-turn routing decisions to the frontend as
# notices ("[turn → haiku]" / "[turn → sonnet]").
VERBOSE_MODE = _env_flag("AIME_VERBOSE", False)


def load_system_prompt(path: str = SYSTEM_PROMPT_PATH) -> str:
    with open(path) as f:
        return f.read()
