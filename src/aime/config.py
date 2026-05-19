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
]

AGENT_MODEL = "claude-sonnet-4-6"


def load_system_prompt(path: str = SYSTEM_PROMPT_PATH) -> str:
    with open(path) as f:
        return f.read()
