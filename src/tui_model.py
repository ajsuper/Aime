import json
import os
import hashlib
import requests
from anthropic import Anthropic
import datetime
import calendar
from datetime import date
import re

from rich.markup import render as render_markup, escape as escape_markup
from rich.errors import MarkupError

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, VerticalScroll
from textual.widgets import (
    Footer,
    Header,
    Input,
    Static,
    RichLog,
    Button,
    ContentSwitcher,
    Tabs,
    Tab,
    DataTable,
)

MonthSelected = "04"
client = Anthropic(
    max_retries=3
)
API_URL = "http://localhost:8080/api"
CONFIG_PATH = ".agent_config.json"
PREFS_PATH = ".tui_prefs.json"
SYSTEM_PROMPT_PATH = "system_prompt.md"
DEFAULT_THEME = "gruvbox"


def load_prefs() -> dict:
    try:
        with open(PREFS_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_prefs(prefs: dict) -> None:
    try:
        with open(PREFS_PATH, "w") as f:
            json.dump(prefs, f, indent=2)
    except OSError:
        pass

with open(SYSTEM_PROMPT_PATH) as f:
    SYSTEM_PROMPT = f.read()

TOOL_NAME_MAP = {
    "FilterUsersEvents": "get_events",
    "EditEvent": "replace_event",
    "CreateEvent": "create_event",
    "FilterTopics": "get_topics",
    "CreateTopic": "create_topic",
    "ReplaceTopic": "replace_topic",
    "GetTopicContents": "get_topic_contents",
    "ReplaceTopicContents": "replace_topic_contents",
    "EditTopicContents": "edit_topic_contents",
}

MONTH_STR_TO_NUMBER_MAP = {
    "one": "01",
    "two": "02",
    "three": "03",
    "four": "04",
    "five": "05",
    "six": "06",
    "seven": "07",
    "eight": "08",
    "nine": "09",
    "ten": "10",
    "eleven": "11",
    "twelve": "12",
}

MONTH_NUMBER_TO_STR_MAP = [
    "", "one", "two", "three", "four", "five", "six",
    "seven", "eight", "nine", "ten", "eleven", "twelve",
]

SCHEMA_FILES = [
    "api_request_schema.json",
    "api_replace_event_schema.json",
    "api_create_event_schema.json",
    "api_request_topics_schema.json",
    "api_create_topic_schema.json",
    "api_replace_topic_schema.json",
    "api_get_topic_contents_schema.json",
    "api_replace_topic_contents_schema.json",
    "api_edit_topic_contents_schema.json",
]

AGENT_MODEL = "claude-sonnet-4-6"


def _truncate_for_log(value, limit: int = 60) -> str:
    if value is None:
        return ""
    s = str(value).replace("\n", " ").strip()
    if len(s) <= limit:
        return s
    return s[: limit - 1] + "…"


def _format_tool_details(name: str, inp: dict) -> str:
    """Return a one-line description of what a tool call is doing, based on
    its input. Empty string if nothing notable."""
    if not isinstance(inp, dict):
        return ""
    parts: list[str] = []

    if name == "FilterUsersEvents":
        if inp.get("filter_by_date"):
            parts.append(
                f"date {inp.get('start_date', '?')} → {inp.get('end_date', '?')}"
            )
        if inp.get("filter_by_category"):
            parts.append(f"category={inp.get('category')}")
        if inp.get("filter_by_title"):
            parts.append(f"title~'{_truncate_for_log(inp.get('title'), 30)}'")
        if inp.get("sort_order"):
            parts.append(f"sort={inp.get('sort_order')}")
    elif name == "CreateEvent":
        title = _truncate_for_log(inp.get("title"), 40) or "?"
        date_ = inp.get("date") or "?"
        parts.append(f"\"{title}\" on {date_}")
        if inp.get("time"):
            parts.append(f"at {inp['time']}")
        if inp.get("category"):
            parts.append(f"#{inp['category']}")
    elif name == "EditEvent":
        parts.append(f"id={inp.get('id', '?')}")
        for field in ("title", "date", "time", "category", "summary"):
            if inp.get(field):
                parts.append(f"{field}={_truncate_for_log(inp[field], 30)}")
    elif name == "FilterTopics":
        if inp.get("filter_by_category"):
            parts.append(f"category={inp.get('category')}")
        if inp.get("filter_by_title"):
            parts.append(f"title~'{_truncate_for_log(inp.get('title'), 30)}'")
        if not parts:
            parts.append("listing all")
    elif name == "CreateTopic":
        parts.append(f"\"{_truncate_for_log(inp.get('title'), 40) or '?'}\"")
        if inp.get("category"):
            parts.append(f"#{inp['category']}")
    elif name == "ReplaceTopic":
        parts.append(f"id={inp.get('id', '?')}")
        if inp.get("title"):
            parts.append(f"title={_truncate_for_log(inp['title'], 30)}")
    elif name == "GetTopicContents":
        parts.append(f"id={inp.get('id', '?')}")
    elif name == "ReplaceTopicContents":
        parts.append(f"id={inp.get('id', '?')}")
        if inp.get("contents") is not None:
            parts.append(f"len={len(str(inp['contents']))}")
    elif name == "EditTopicContents":
        parts.append(f"id={inp.get('id', '?')}")
        if "old_string" in inp:
            parts.append(f"old='{_truncate_for_log(inp.get('old_string'), 30)}'")
        if "new_string" in inp:
            parts.append(f"new='{_truncate_for_log(inp.get('new_string'), 30)}'")
    else:
        # Built-in / unknown tools — best-effort common keys.
        for key in ("query", "url", "command", "path", "pattern"):
            if inp.get(key):
                parts.append(f"{key}=\"{_truncate_for_log(inp[key], 60)}\"")
        if not parts:
            keys = [k for k in inp.keys() if k != "tool_name"][:3]
            if keys:
                parts.append("with " + ", ".join(keys))

    return ", ".join(parts)


def _format_tool_response(name: str, result) -> str:
    """Return a one-line description of what a tool call returned, based on
    its result. Mirrors _format_tool_details. Empty string if nothing notable."""
    if isinstance(result, dict) and "error" in result:
        return f"error: {_truncate_for_log(result.get('error'), 80)}"

    parts: list[str] = []

    if name in ("FilterUsersEvents", "FilterTopics"):
        if isinstance(result, list):
            items = result
        elif isinstance(result, dict):
            items = result.get("events") or result.get("topics") or []
        else:
            items = []
        label = "event" if name == "FilterUsersEvents" else "topic"
        parts.append(f"{len(items)} {label}{'s' if len(items) != 1 else ''}")
        for it in items[:3]:
            if not isinstance(it, dict):
                continue
            title = _truncate_for_log(it.get("title") or it.get("name"), 30) or "?"
            if name == "FilterUsersEvents":
                parts.append(f"\"{title}\"@{it.get('date', '?')}")
            else:
                tid = it.get("id")
                parts.append(f"\"{title}\"" + (f"#{tid}" if tid is not None else ""))
        if len(items) > 3:
            parts.append(f"+{len(items) - 3} more")
    elif name in ("CreateEvent", "EditEvent", "CreateTopic", "ReplaceTopic",
                  "ReplaceTopicContents", "EditTopicContents"):
        if isinstance(result, dict):
            for key in ("id", "status", "ok", "success"):
                if key in result:
                    parts.append(f"{key}={_truncate_for_log(result[key], 30)}")
            if not parts:
                keys = list(result.keys())[:3]
                if keys:
                    parts.append("keys: " + ", ".join(keys))
    elif name == "GetTopicContents":
        if isinstance(result, dict):
            contents = result.get("contents", "") or ""
            parts.append(f"len={len(str(contents))}")
            preview = _truncate_for_log(contents, 50)
            if preview:
                parts.append(f"preview='{preview}'")
    else:
        # Built-in / unknown tools — best-effort summary.
        if isinstance(result, list):
            parts.append(f"{len(result)} item{'s' if len(result) != 1 else ''}")
        elif isinstance(result, dict):
            for k, v in list(result.items())[:3]:
                parts.append(f"{k}={_truncate_for_log(v, 40)}")
        else:
            preview = _truncate_for_log(result, 60)
            if preview:
                parts.append(preview)

    return ", ".join(parts)


user_first_interaction = True

SPECIAL_TOPICS = [
    {
        "title": "About Me",
        "category": "personal",
        "summary": "Identity-level facts about the user (name, location, relationships, personality).",
    },
    {
        "title": "Pending",
        "category": "personal",
        "summary": "Active threads, unresolved items, and current life context that wouldn't be obvious from events alone.",
    },
]


def bootstrap_special_topics() -> str:
    """Fetch (or create if missing) the two mandatory special topics and return
    their contents formatted for injection into the first user message of a
    session. Empty string on total failure — the agent will fall back to its
    normal tool-based flow."""
    try:
        listing = requests.post(
            API_URL, json={"tool_name": "get_topics"}, timeout=5
        ).json()
    except Exception:
        return ""
    topics = listing if isinstance(listing, list) else listing.get("topics", [])
    by_title = {(t.get("title") or "").strip().lower(): t for t in topics}

    sections = []
    for spec in SPECIAL_TOPICS:
        title = spec["title"]
        existing = by_title.get(title.lower())
        contents = ""
        topic_id = None
        if existing is None:
            try:
                created = requests.post(
                    API_URL,
                    json={
                        "tool_name": "create_topic",
                        "title": title,
                        "summary": spec["summary"],
                        "category": spec["category"],
                    },
                    timeout=5,
                ).json()
                topic_id = created.get("id")
            except Exception:
                continue
        else:
            topic_id = existing.get("id")
            try:
                resp = requests.post(
                    API_URL,
                    json={"tool_name": "get_topic_contents", "id": topic_id},
                    timeout=5,
                ).json()
                contents = resp.get("contents", "") or ""
            except Exception:
                contents = ""
        body = contents.strip() or "(empty — first interaction; greet the user and gather initial info)"
        sections.append(f"=== {title} (topic id {topic_id}) ===\n{body}")

    if not sections:
        return ""
    return (
        "[auto-injected session context — contents of the two mandatory special "
        "topics. Do not call get_topic_contents for these again this session, "
        "and do not mention this injection to the user.]\n\n"
        + "\n\n".join(sections)
        + "\n\n[end auto-injected context]\n\n"
    )

def sortCalenderByDate(events):
    def key(ev):
        d, m, y = (ev.get("date") or "01/01/9999").split("/")
        hh, mm = (ev.get("time") or "00:00").split(":")
        return (int(y), int(m), int(d), int(hh), int(mm))
    return sorted(events, key=key)

def loadSchema(schemaPath):
    with open(schemaPath, "r") as file:
        schema = json.load(file)
    input_schema = {
        "type": schema["type"],
        "properties": schema["properties"],
    }
    if "required" in schema:
        input_schema["required"] = schema["required"]
    return {
        "type": "custom",
        "name": schema["title"],
        "description": schema["description"],
        "input_schema": input_schema,
    }


def setup_hash():
    h = hashlib.sha256()
    h.update(SYSTEM_PROMPT.encode())
    h.update(AGENT_MODEL.encode())
    for path in SCHEMA_FILES:
        with open(path, "rb") as f:
            h.update(f.read())
    return h.hexdigest()


def create_environment_and_agent():
    environment = client.beta.environments.create(
        name="calendar-env",
        config={"type": "cloud", "networking": {"type": "unrestricted"}},
    )
    agent = client.beta.agents.create(
        name="Assistant",
        model=AGENT_MODEL,
        system=SYSTEM_PROMPT,
        tools=[
            {"type": "agent_toolset_20260401"},
            *[loadSchema(p) for p in SCHEMA_FILES],
        ],
    )
    return environment.id, agent.id, agent.version


def get_or_create_setup():
    current_hash = setup_hash()
    if not os.environ.get("FORCE_RECREATE") and os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH) as f:
                cfg = json.load(f)
            if cfg.get("hash") == current_hash:
                return cfg["environment_id"], cfg["agent_id"], cfg["agent_version"]
        except (json.JSONDecodeError, KeyError):
            pass
    env_id, agent_id, agent_version = create_environment_and_agent()
    with open(CONFIG_PATH, "w") as f:
        json.dump(
            {
                "environment_id": env_id,
                "agent_id": agent_id,
                "agent_version": agent_version,
                "hash": current_hash,
            },
            f,
            indent=2,
        )
    return env_id, agent_id, agent_version


environment_id, agent_id, agent_version = get_or_create_setup()

def create_session():
    global environment_id, agent_id, agent_version
    try:
        session = client.beta.sessions.create(
            agent={"type": "agent", "id": agent_id, "version": agent_version},
            environment_id=environment_id,
        )
    except Exception:
        if os.path.exists(CONFIG_PATH):
            os.remove(CONFIG_PATH)
        environment_id, agent_id, agent_version = get_or_create_setup()
        session = client.beta.sessions.create(
            agent={"type": "agent", "id": agent_id, "version": agent_version},
            environment_id=environment_id,
        )
    return session

session = create_session()

# ---------- UI ----------

class AssistantView(Container):
    """Chat transcript + input. Pipes user messages into the agent session and
    renders streamed agent replies."""

    def compose(self) -> ComposeResult:
        yield RichLog(id="transcript", wrap=True, markup=True, auto_scroll=True)
        yield Input(placeholder="Message Aime…  (enter to send)", id="prompt")

    def on_mount(self) -> None:
        log = self.query_one("#transcript", RichLog)
        log.write(
            "[bold green]Aime ready.[/bold green] "
            "[dim]Ctrl+A assistant · Ctrl+S calendar · Ctrl+T topics · Ctrl+Q quit[/dim]\n"
        )

    def on_input_submitted(self, event: Input.Submitted) -> None:
        log = self.query_one("#transcript", RichLog)
        text = event.value.strip()
        if (text == ":q"):
            application.exit()
            exit()
            return
        if (text == "/reset"): # Reset model.
            global session, user_first_interaction
            event.input.value = ""
            try:
                client.beta.sessions.terminate(session_id=session.id)
            except Exception:
                pass
            session = create_session()
            user_first_interaction = True
            self.app._assistant_prefixed = False
            self.app._thinking_visible = False
            self.app._is_idle = True
            self.app._pending_user_messages = []
            log.clear()
            log.write("[yellow] The current conversation has ended because you typed '/reset'. Begin a new conversation. [/yellow]")
            self.app.run_worker(
                self.app._stream_events,
                thread=True,
                exclusive=True,
                name="agent-stream",
            )
            return
        if (text == "/toggle_log_model_thinking"):
            self.app._log_model_thinking = not self.app._log_model_thinking
            log.write(f"[dim]Log model thinking set to: {self.app._log_model_thinking}[/dim]")
            event.input.value = ""
            return
        if not text:
            return
        event.input.value = ""
        self.app.send_user_message(text)

    def focus_input(self) -> None:
        self.query_one("#prompt", Input).focus()


class VimDataTable(DataTable):
    """DataTable with vim-style hjkl navigation in addition to arrow keys.

    After any cursor movement (arrow keys or hjkl), dispatches to a direction-
    specific callback on the enclosing CalendarView, passing the table and the
    Tabs widget so the callback can read/update either.
    """

    BINDINGS = [
        Binding("h", "cursor_left", "Left", show=False),
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("l", "cursor_right", "Right", show=False),
    ]

    def _dispatch_cursor(self, direction: str) -> None:
        view = self.screen.query_one(CalendarView)
        tabs = view.query_one(Tabs)
        handler = getattr(view, f"on_cursor_{direction}", None)
        if handler is not None:
            handler(self, tabs)

    def action_cursor_up(self) -> None:
        if self.cursor_row == 0:
            tabs = self.screen.query_one(CalendarView).query_one(Tabs)
            tabs.focus()
            return
        super().action_cursor_up()
        self._dispatch_cursor("up")

    def action_cursor_down(self) -> None:
        super().action_cursor_down()
        self._dispatch_cursor("down")

    def action_cursor_left(self) -> None:
        super().action_cursor_left()
        self._dispatch_cursor("left")

    def action_cursor_right(self) -> None:
        super().action_cursor_right()
        self._dispatch_cursor("right")

class VimTabs(Tabs):
    """Tabs with vim-style h/l and a j/down binding that drops focus into the
    calendar's DataTable, so arrow/vim keys flow seamlessly between the two."""

    BINDINGS = [
        Binding("h", "previous_tab", "Previous tab", show=False),
        Binding("l", "next_tab", "Next tab", show=False),
        Binding("j", "focus_table", "Focus table", show=False),
        Binding("down", "focus_table", "Focus table", show=False),
    ]

    def action_focus_table(self) -> None:
        table = self.screen.query_one(CalendarView).query_one(VimDataTable)
        table.focus()

class CalendarView(Container):
    """Direct view of the events store. Hits the same /api endpoint the agent
    uses, with tool_name=get_events."""

    selected_day: int | None = None
    current_date = datetime.datetime.now()
    month_name = calendar.month_name[current_date.month]
    day_int = current_date.day

    def compose(self) -> ComposeResult:
        with Horizontal(id="calendar-toolbar"):
            yield Static("[bold]Your events[/bold]", id="calendar-title")
            yield Button("Refresh", id="calendar-refresh", variant="primary")
        yield VimTabs(
            Tab("Jan", id="one"),
            Tab("Feb", id="two"),
            Tab("Mar", id="three"),
            Tab("Apr", id="four"),
            Tab("May", id="five"),
            Tab("Jun", id="six"),
            Tab("Jul", id="seven"),
            Tab("Aug", id="eight"),
            Tab("Sep", id="nine"),
            Tab("Oct", id="ten"),
            Tab("Nov", id="eleven"),
            Tab("Dec", id="twelve"),
        )
        with Horizontal(id="calendar-body"):
            yield VimDataTable()
            yield VerticalScroll(Static("", id="calendar-list"), id="calendar-scroll")

    def on_tabs_tab_activated(self, event: Tabs.TabActivated) -> None:
        global MonthSelected
        """Handle TabActivated message sent by Tabs."""
        tabs = self.query_one(Tabs)
        active_tab = tabs.active_tab
        if active_tab is not None:
            MonthSelected = MONTH_STR_TO_NUMBER_MAP[active_tab.id]
            self.refresh_events()
            self.refresh_table()

    def on_cursor_up(self, table: "VimDataTable", tabs: Tabs) -> None:
        """Called after the calendar cursor moves up. Fill in custom behavior."""
        pass

    def on_cursor_down(self, table: "VimDataTable", tabs: Tabs) -> None:
        """Called after the calendar cursor moves down. Fill in custom behavior."""
        pass

    def on_cursor_left(self, table: "VimDataTable", tabs: Tabs) -> None:
        """Called after the calendar cursor moves left. Fill in custom behavior."""
        pass

    def on_cursor_right(self, table: "VimDataTable", tabs: Tabs) -> None:
        """Called after the calendar cursor moves right. Fill in custom behavior."""
        pass

    def on_data_table_cell_selected(self, event: DataTable.CellSelected) -> None:
        try:
            first_line = render_markup(str(event.value)).plain.split("\n", 1)[0]
            first_line = first_line.strip()
            day_number_selected = int(re.search(r'\d+', first_line).group())
        except (ValueError, MarkupError, AttributeError):
            self.selected_day = 0
            return
        if day_number_selected < 1:
            return
        self.selected_day = day_number_selected
        self.refresh_events()

    def refresh_table(self):
        table = self.query_one(DataTable)
        table.clear(columns=True)

        total_w = table.size.width
        col_w = max(3, total_w // 9) + 1

        for day in ("Sunday", "Monday", "Tuesday", "Wednesday",
                    "Thursday", "Friday", "Saturday"):
            table.add_column(day, width=col_w)

        titles_by_day: dict[int, list[str]] = {}
        try:
            response = requests.post(
                API_URL,
                json={"tool_name": "get_events",
                      "sort_order": "asc",
                      "filter_by_date": True,
                      "start_date": "00/" + MonthSelected + "/2026",
                      "end_date": "40/" + MonthSelected + "/2026",
                      },
                timeout=5,
            )
            if response.ok:
                data = response.json()
                events = data if isinstance(data, list) else data.get("events", [])
                for ev in events:
                    event_date = ev.get("date", "")
                    title = ev.get("title") or ev.get("name") or "(untitled)"

                    try:
                        day_num = int(event_date.split("/")[0])
                    except (ValueError, IndexError):
                        continue
                    titles_by_day.setdefault(day_num, []).append(title)

        except Exception:
            pass

        first_date = date(2026, int(MonthSelected), 1)
        first_day_of_month = first_date.isoweekday()

        total_h = table.size.height or 16
        row_h = max(1, (total_h - 1) // 6) + 1
        day_numbers = [1, 2, 3, 4, 5, 6, 7]
        for d in range(7):
            day_numbers[d] = max(0, day_numbers[d] - first_day_of_month)

        for _ in range(6):
            cells = []
            for n in day_numbers: # n is the day number, it corresponds to the day_numbers list. Could be done without list but oh well.
                try:
                    cell = ""
                    # will return error if date is not in the month. This causes the date to empty which is desired.
                    validation_date = date(2026, int(MonthSelected), n)
                    if (self.day_int == n and int(MonthSelected) == self.current_date.month):
                        cell = "[bold white blink] >" + str(n) + "< today...[/bold white blink]"
                    else:
                        cell = "[grey]" + str(n) + "[/grey]"
                except:
                    cell = "_"
                for title in titles_by_day.get(n, []):
                    if len(title) > 20:
                        cell += f"\n[dim]•{title[:20]}...[/dim]"
                    else:
                        cell += f"\n[dim]•{title}[/dim]"
                cells.append(cell)
            table.add_row(*cells, height=row_h)
            new_week_first_day = day_numbers[6] + 1
            for i in range(7):
                day_numbers[i] = new_week_first_day + i

        table.zebra_stripes = True

    def on_mount(self) -> None:
        self.refresh_events()
        self.refresh_table()
        tabs = self.query_one(Tabs)
        tabs.active = MONTH_NUMBER_TO_STR_MAP[self.current_date.month]

    def focus_table(self) -> None:
        self.query_one(VimDataTable).focus()

    def on_resize(self, event) -> None:
        self.refresh_table()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "calendar-refresh":
            self.refresh_events()
            self.refresh_table()

    def refresh_events(self) -> None:
        target = self.query_one("#calendar-list", Static)
        target.update("[dim]loading…[/dim]")
        if (self.selected_day is None) or (self.selected_day == 0):
            target.update("[dim]select a day in the calendar[/dim]")
            if (self.selected_day == 0):
                target.update("[dim] Day does not belong to this month[/dim]")
            return
        # Should only run if it's a valid day in the month.
        day_str = f"{self.selected_day:02d}"
        try:
            response = requests.post(
                API_URL,
                json={"tool_name": "get_events",
                      "sort_order": "asc",
                      "filter_by_date": True,
                      "start_date": f"{day_str}/{MonthSelected}/2026",
                      "end_date": f"{day_str}/{MonthSelected}/2026",
                      },
                timeout=5,
            )
            data = response.json() if response.ok else {"error": response.text}
        except Exception as exc:
            target.update(f"[red]error:[/red] {exc}")
            return

        events = data if isinstance(data, list) else data.get("events", [])
        if not events:
            target.update(f"[dim]no events on {day_str}/{MonthSelected}[/dim]")
            return

        sortCalenderByDate(events)
        lines = []
        for ev in events:
            date = ev.get("date", "")
            time_ = ev.get("time", "")
            title = ev.get("title") or ev.get("name") or "(untitled)"
            category = ev.get("category", "")
            summary = ev.get("summary", "")
            header = f"[bold cyan]{date}[/bold cyan]"
            if time_:
                header += f" [cyan]{time_}[/cyan]"
            if category:
                header += f"  [dim]#{category}[/dim]"
            lines.append(f"{header}\n  [bold]{title}[/bold]")
            if summary:
                lines.append(f"  [dim]{summary}[/dim]")
            lines.append("")
        target.update("\n".join(lines).rstrip())

class TopicView(Container):
    """Direct view of the topics store. tool_name=get_topics."""

    def compose(self) -> ComposeResult:
        with Horizontal(id="topic-toolbar"):
            yield Static("[bold]Your topics[/bold]", id="topic-title")
            yield Button("Refresh", id="topic-refresh", variant="primary")
        yield VerticalScroll(Static("", id="topic-list"), id="topic-scroll")

    def on_mount(self) -> None:
        self.refresh_topics()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "topic-refresh":
            self.refresh_topics()

    def refresh_topics(self) -> None:
        target = self.query_one("#topic-list", Static)
        target.update("[dim]loading…[/dim]")
        try:
            response = requests.post(
                API_URL, json={"tool_name": "get_topics"}, timeout=5
            )
            data = response.json() if response.ok else {"error": response.text}
        except Exception as exc:
            target.update(f"[red]error:[/red] {exc}")
            return

        topics = data if isinstance(data, list) else data.get("topics", [])
        if not topics:
            target.update("[dim]no topics yet[/dim]")
            return

        lines = []
        for tp in topics:
            title = tp.get("title") or tp.get("name") or "(untitled)"
            category = tp.get("category", "")
            summary = tp.get("summary", "")
            head = f"[bold cyan]{title}[/bold cyan]"
            if category:
                head += f"  [dim]#{category}[/dim]"
            lines.append(head)
            if summary:
                lines.append(f"  [dim]{summary}[/dim]")
            lines.append("")
        target.update("\n".join(lines).rstrip())


class Aime(App):
    CSS_PATH = "user_prompt.css"
    TITLE = "Aime"
    SUB_TITLE = "an extension of your mind"

    BINDINGS = [
        Binding("ctrl+a", "mode('assistant')", "Assistant"),
        Binding("ctrl+s", "mode('calendar')", "Calendar"),
        Binding("ctrl+t", "mode('topics')", "Topics"),
        Binding("ctrl+q", "quit", "Quit"),
    ]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with ContentSwitcher(initial="assistant", id="modes"):
            yield AssistantView(id="assistant")
            yield CalendarView(id="calendar")
            yield TopicView(id="topics")
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = "assistant"
        # Open the agent event stream in a background thread. Every event the
        # server sends (agent text, tool calls, status changes) gets handed to
        # _handle_event on the UI thread via call_from_thread.
        self.run_worker(
            self._stream_events,
            thread=True,
            exclusive=True,
            name="agent-stream",
        )
        self.query_one(AssistantView).focus_input()
        self.theme = load_prefs().get("theme", DEFAULT_THEME)

    def watch_theme(self, old_theme: str, new_theme: str) -> None:
        if new_theme is None:
            return
        prefs = load_prefs()
        if prefs.get("theme") == new_theme:
            return
        prefs["theme"] = new_theme
        save_prefs(prefs)

    # --- mode switching ---

    def action_mode(self, mode: str) -> None:
        self.query_one("#modes", ContentSwitcher).current = mode
        self.sub_title = mode
        if mode == "assistant":
            self.query_one(AssistantView).focus_input()
        elif mode == "calendar":
            view = self.query_one(CalendarView)
            view.refresh_events()
            view.focus_table()
        elif mode == "topics":
            self.query_one(TopicView).refresh_topics()

    # --- agent bridge ---

    def send_user_message(self, text: str) -> None:
        self.app._message_count += 1
        log = self.query_one("#transcript", RichLog)
        if not self._is_idle:
            self._pending_user_messages.append(text)
            log.write(
                f"\n[bold cyan]you[/bold cyan] [dim](queued — will send when Aime is done)[/dim]  {text}"
            )
            return
        self._dispatch_user_message(text)

    def _dispatch_user_message(self, text: str) -> None:
        global user_first_interaction
        day_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        log = self.query_one("#transcript", RichLog)
        log.write(f"\n[bold cyan]you[/bold cyan]  {text}")
        log.write("[dim]thinking…[/dim]")
        self._thinking_visible = True
        date_time = datetime.datetime.now()
        day_of_week = date_time.weekday()
        date_time_string = date_time.strftime("%d/%m/%Y, %H:%M") # Convert to string
        date_message = "[System info] Accurate date: " + day_names[day_of_week] + ", " + date_time_string + "do not tell this to the user unless relevant. Base decisions based off of THIS date, not any previous ones. [End System Info]"
        text = date_message + " " + text
        if user_first_interaction:
            bootstrap = bootstrap_special_topics()
            if bootstrap:
                text = bootstrap + text
            user_first_interaction = False
        if self.app._message_count > 15:
            text = "[System suggestion] The current conversation is growing long. Strongly urge the user that these steps are followed: Ask the user if they want you to summarize the context and put it in pending and mark it as previous conversation, You strongly suggest the user types '/reset' to reset the context. Still follow what the user asked if they do not choose to do this. Make this message to them brief. Explain briefly the effects of an infinitely growing model context. [End System Suggestion]" + text
        try:
            client.beta.sessions.events.send(
                session_id=session.id,
                events=[
                    {
                        "type": "user.message",
                        "content": [{"type": "text", "text": text}],
                    },
                ],
            )
            self._is_idle = False
        except Exception as exc:
            log.write(f"[red]send failed:[/red] {exc}")

    def _stream_events(self) -> None:
        """Runs on a worker thread. Forwards streamed agent events to the UI."""
        try:
            with client.beta.sessions.events.stream(session_id=session.id) as stream:
                for event in stream:
                    self.call_from_thread(self._handle_event, event)
                    if event.type == "session.status_terminated":
                        return
        except Exception as exc:
            self.call_from_thread(
                self._log_line, f"[red]stream error:[/red] {exc}"
            )

    _thinking_visible = False
    _log_model_thinking = False
    _assistant_prefixed = False
    _message_count = 0
    _is_idle = True
    _pending_user_messages: list[str] = []

    def _clear_thinking(self) -> None:
        if self._thinking_visible:
            # RichLog can't retract a line, so we just drop a small separator
            # the next time real text arrives. (The "thinking…" stays as part
            # of the scrollback — acceptable for a transcript.)
            self._thinking_visible = False

    def _log_line(self, text: str) -> None:
        self.query_one("#transcript", RichLog).write(text)

    def _safe_write(self, log: RichLog, text: str) -> None:
        try:
            log.write(render_markup(text))
        except Exception:
            log.write(escape_markup(text))
            log.write("[bold red] Pretty output is disabled. Model made small mistake in formatting response.[/bold red]")

    def _handle_event(self, event) -> None:
        log = self.query_one("#transcript", RichLog)

        #if event.type == "session.status_idle":
        #log.write(f"[red]stop_reason: {event.stop_reason.type}[/red]")
        if event.type == "agent.message":
            for block in event.content:
                if block.type == "text":
                    self._clear_thinking()
                    if not self._assistant_prefixed:
                        log.write("[bold red]aime[/bold red]")
                        self._assistant_prefixed = True
                    self._safe_write(log, block.text)

        if event.type == "agent.thinking" and self.app._log_model_thinking:
            for block in event.content:
                if block.type == "text":
                    self._clear_thinking()
                    log.write(f"[dim]{block.text}[/dim]")

        elif event.type == "agent.custom_tool_use":
            self._clear_thinking()
            input_dict = dict(event.input) if event.input else {}
            details = _format_tool_details(event.name, input_dict)
            detail_str = f" [dim italic]· {escape_markup(details)}[/dim italic]" if details else ""
            log.write(
                f"[dim] Waiting on tool: [/dim][cyan]{event.name}[/cyan][dim]…[/dim]{detail_str}"
            )
            payload = dict(input_dict)
            payload["tool_name"] = TOOL_NAME_MAP.get(event.name, event.name)
            try:
                response = requests.post(API_URL, json=payload, timeout=10)
                result = (
                    response.json() if response.ok else {"error": response.text}
                )
            except Exception as exc:
                result = {"error": str(exc)}
            response_details = _format_tool_response(event.name, result)
            if response_details:
                log.write(
                    f"[dim] Tool result: [/dim][green]{event.name}[/green][dim italic] · {escape_markup(response_details)}[/dim italic]"
                )
            try:
                client.beta.sessions.events.send(
                    session_id=session.id,
                    events=[
                        {
                            "type": "user.custom_tool_result",
                            "custom_tool_use_id": event.id,
                            "content": [
                                {"type": "text", "text": json.dumps(result)}
                            ],
                        }
                    ],
                )
            except Exception as exc:
                log.write(f"[red]tool result send failed:[/red] {exc}")
        elif event.type == "agent.tool_use":
            self._clear_thinking()
            input_dict = dict(event.input) if getattr(event, "input", None) else {}
            details = _format_tool_details(event.name, input_dict)
            detail_str = f" [dim italic]· {escape_markup(details)}[/dim italic]" if details else ""
            log.write(
                f"[dim] Waiting on tool: [/dim][cyan]{event.name}[/cyan][dim]…[/dim]{detail_str}"
            )

        elif event.type == "session.status_idle":
            if event.stop_reason.type == "end_turn":
                # Turn finished. Reset the "aime" prefix so the next reply gets one.
                self._assistant_prefixed = False
                self._is_idle = True
                if self._pending_user_messages:
                    next_text = self._pending_user_messages.pop(0)
                    self._dispatch_user_message(next_text)
                else:
                    log.write("[dim]_____________________[/dim]")
                    log.write("[bold green]Aime ready[/bold green]")


        elif event.type == "session.status_terminated":
            log.write("[red]session terminated[/red]")

if __name__ == "__main__":
    Aime().run()
