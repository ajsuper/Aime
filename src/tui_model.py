"""Textual frontend for Aime.

This module is intentionally thin: it composes widgets and renders the
`CoreEvent` stream emitted by `aime.ConversationController`. All
conversation logic, tool execution, onboarding, and command parsing live in
the `aime` package, so a different frontend (CLI, web, etc.) can be built
against the same controller without touching this file.
"""

import json
import os
import datetime
import calendar
import re
import threading
from datetime import date

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
from textual.geometry import Offset, Region, Spacing
from textual_autocomplete import AutoComplete, DropdownItem

from provider_backend import AnthropicMessagesBackend

from aime import (
    ConversationController,
    CoreEvent,
    ToolGateway,
    CalendarService,
    TopicService,
    config as aime_config,
)
from aime.services import sort_events_by_date


# --- TUI-only settings ---

PREFS_PATH = os.environ['HOME'] + "/.config/aime-assistant/tui_prefs.json"
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


# --- calendar tab labels ---

MONTH_STR_TO_NUMBER_MAP = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}

MONTH_NUMBER_TO_STR_MAP = [
    "", "one", "two", "three", "four", "five", "six",
    "seven", "eight", "nine", "ten", "eleven", "twelve",
]


# ============================================================================
# Widgets
# ============================================================================

def _make_slash_command_candidates(app: "Aime"):
    """Factory for the slash-command AutoComplete candidate callable.

    Returns the static slash commands plus one `/load <id>` entry per saved
    conversation. Each saved-conversation item *displays and matches* on the
    conversation's summary; the actual command to insert lives in the item
    `id` — see `CommandAutoComplete._complete`.
    """
    def candidates(_state) -> list[DropdownItem]:
        items = [
            DropdownItem("/reset", prefix="⟳  ", id="/reset"),
            DropdownItem("/load", prefix="↺  ", id="/load"),
        ]
        for session in app._controller.list_sessions():
            label = session.summary or "(untitled conversation)"
            items.append(
                DropdownItem(f"/load  —  {label}", prefix="↺  ", id=f"/load {session.id}")
            )
        return items
    return candidates


class CommandAutoComplete(AutoComplete):
    """AutoComplete variant for the slash-command input.

    Two tweaks over the stock widget:
      * completion inserts the item's `id` (the real command) rather than its
        displayed `main` text, so dropdown items can show a friendly title
        while still inserting `/load <session-id>`.
      * the dropdown is positioned *above* the cursor instead of below.
    """

    def _complete(self, option_index: int) -> None:
        if not self.display or self.option_list.option_count == 0:
            return
        option = self.option_list.get_option_at_index(option_index)
        value = option.id or option.value
        with self.prevent(Input.Changed):
            self.apply_completion(value, self._get_target_state())
        self.post_completion()

    def should_show_dropdown(self, search_string: str) -> bool:
        # Only autocomplete slash commands; otherwise plain prose like "yes"
        # would fuzzy-match into "/reset" and trigger an unwanted completion.
        if not search_string.startswith("/"):
            return False
        return super().should_show_dropdown(search_string)

    def _align_to_target(self) -> None:
        x, y = self.target.cursor_screen_offset
        width, height = self.option_list.outer_size
        # `y - height` places the dropdown above the cursor line; constrain()
        # still keeps it on-screen if there isn't room above.
        x, y, _width, _height = Region(x - 1, y - height, width, height).constrain(
            "inside",
            "none",
            Spacing.all(0),
            self.screen.scrollable_content_region,
        )
        self.absolute_offset = Offset(x, y)


class AssistantView(Container):
    """Chat transcript + input. Forwards user input to the controller and
    renders streamed agent replies."""

    def compose(self) -> ComposeResult:
        yield RichLog(id="transcript", wrap=True, markup=True, auto_scroll=True)
        user_input = Input(placeholder="Message Aime…  (enter to send)", id="prompt")
        yield user_input
        yield CommandAutoComplete(
            user_input,
            candidates=_make_slash_command_candidates(self.app),
        )

    def on_mount(self) -> None:
        log = self.query_one("#transcript", RichLog)
        log.write(
            "[bold green]Aime ready.[/bold green] "
            "[dim]Ctrl+A assistant · Ctrl+S calendar · Ctrl+T topics · Ctrl+Q quit[/dim]\n"
        )

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value
        event.input.value = ""
        should_quit = self.app._controller.dispatch_input(text)
        if should_quit:
            self.app.exit()

    def focus_input(self) -> None:
        self.query_one("#prompt", Input).focus()


class VimDataTable(DataTable):
    """DataTable with vim-style hjkl navigation in addition to arrow keys."""

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
    """Direct view of the events store, via CalendarService."""

    selected_day: int | None = None
    current_date = datetime.datetime.now()
    # Month/year currently shown in the grid. The selected_month starts on the
    # real current month; year stays at the current year (no historical view
    # in the current TUI).
    selected_month: int = current_date.month
    selected_year: int = current_date.year

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
        tabs = self.query_one(Tabs)
        active_tab = tabs.active_tab
        if active_tab is not None:
            self.selected_month = MONTH_STR_TO_NUMBER_MAP[active_tab.id]
            self.refresh_events()
            self.refresh_table()

    def on_cursor_up(self, table: "VimDataTable", tabs: Tabs) -> None:
        pass

    def on_cursor_down(self, table: "VimDataTable", tabs: Tabs) -> None:
        pass

    def on_cursor_left(self, table: "VimDataTable", tabs: Tabs) -> None:
        pass

    def on_cursor_right(self, table: "VimDataTable", tabs: Tabs) -> None:
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

    def refresh_table(self) -> None:
        table = self.query_one(DataTable)
        table.clear(columns=True)

        total_w = table.size.width
        col_w = max(3, total_w // 9) + 1

        for day in ("Sunday", "Monday", "Tuesday", "Wednesday",
                    "Thursday", "Friday", "Saturday"):
            table.add_column(day, width=col_w)

        titles_by_day: dict[int, list[str]] = {}
        try:
            events = self.app._calendar_service.events_for_month(
                self.selected_year, self.selected_month
            )
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

        first_date = date(self.selected_year, self.selected_month, 1)
        first_day_of_month = first_date.isoweekday()

        total_h = table.size.height or 16
        row_h = max(1, (total_h - 1) // 6) + 1
        day_numbers = [1, 2, 3, 4, 5, 6, 7]
        for d in range(7):
            day_numbers[d] = max(0, day_numbers[d] - first_day_of_month)

        for _ in range(6):
            cells = []
            for n in day_numbers:
                try:
                    # Will raise if the date isn't valid in this month — which
                    # is the signal to render an empty/placeholder cell.
                    date(self.selected_year, self.selected_month, n)
                    if (self.current_date.day == n
                            and self.selected_month == self.current_date.month
                            and self.selected_year == self.current_date.year):
                        cell = f"[bold white blink] >{n}< today...[/bold white blink]"
                    else:
                        cell = f"[grey]{n}[/grey]"
                except Exception:
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
            if self.selected_day == 0:
                target.update("[dim] Day does not belong to this month[/dim]")
            return
        try:
            events = self.app._calendar_service.events_for_day(
                self.selected_year, self.selected_month, self.selected_day
            )
        except Exception as exc:
            target.update(f"[red]error:[/red] {exc}")
            return

        if not events:
            target.update(
                f"[dim]no events on {self.selected_day:02d}/"
                f"{self.selected_month:02d}[/dim]"
            )
            return

        events = sort_events_by_date(events)
        lines = []
        for ev in events:
            d = ev.get("date", "")
            time_ = ev.get("time", "")
            title = ev.get("title") or ev.get("name") or "(untitled)"
            category = ev.get("category", "")
            summary = ev.get("summary", "")
            header = f"[bold cyan]{d}[/bold cyan]"
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
    """Direct view of the topics store, via TopicService."""

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
            topics = self.app._topic_service.list_topics()
        except Exception as exc:
            target.update(f"[red]error:[/red] {exc}")
            return

        if not topics:
            target.update("[dim]no topics yet[/dim]")
            return

        # Uncategorized topics sink to the end so the alphabetical run stays intact.
        topics = sorted(
            topics,
            key=lambda tp: (not (tp.get("category") or ""), (tp.get("category") or "").lower()),
        )

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


# ============================================================================
# App
# ============================================================================

_NOTICE_COLOR = {
    "info": "white",
    "warning": "yellow",
    "error": "red",
    "success": "green",
}


class Aime(App):
    CSS_PATH = "../resources/style/user_prompt.css"
    TITLE = "Aime"
    SUB_TITLE = "an extension of your mind"

    BINDINGS = [
        Binding("ctrl+a", "mode('assistant')", "Assistant"),
        Binding("ctrl+s", "mode('calendar')", "Calendar"),
        Binding("ctrl+t", "mode('topics')", "Topics"),
        Binding("ctrl+q", "quit", "Quit"),
    ]

    # Presentation state (purely UI; no conversation logic here).
    _thinking_visible = False
    _assistant_prefixed = False
    _stream_buffer: str = ""

    def __init__(self) -> None:
        super().__init__()
        # Remember the main thread so the CoreEvent subscriber can decide
        # whether it needs `call_from_thread` to marshal an event onto the UI.
        self._main_tid = threading.get_ident()

        # The TUI is a single-user local interface with no accounts database
        # and so no wrapped-DEK row to look up. We keep its DEK in a plain
        # key file alongside the data, same machine-bound threat model as
        # the web app's `machine_secret` (see docs/security.md): anyone who
        # has the file can decrypt the data. Web users get the wrapped-DEK
        # path managed by aime.auth instead.
        from aime import encryption as _enc
        tui_user_dir = os.path.join(aime_config.DATABASE_DIR, "users", "1")
        conv_dir = os.path.join(tui_user_dir, "conversations")
        os.makedirs(conv_dir, exist_ok=True)
        dek = _enc.load_or_create_key_file(os.path.join(tui_user_dir, "tui_dek"))

        from aime.model_router import ModelRouter
        from aime.web_search_agent import WebSearchAgent
        from aime import usage as _aime_usage
        from aime import messaging as _aime_messaging

        # The local TUI has no accounts DB, so its messaging destination comes
        # from AIME_MESSAGING_CONTACT in the environment rather than a stored
        # per-account contact (which is the web app's path).
        messaging_contact = _aime_messaging.env_recipient()
        # Messenger = server capability; recipient = the env contact. Separate so
        # the controller can say "not set up" vs "no contact connected" distinctly.
        messenger = _aime_messaging.get_messenger()
        router = ModelRouter(
            haiku_model=aime_config.HAIKU_MODEL,
            sonnet_model=aime_config.SONNET_MODEL,
            router_model=aime_config.ROUTER_MODEL,
            enabled=aime_config.MODEL_ROUTING_ENABLED,
            record_api=_aime_usage.record_api,
        )
        web_search_agent = WebSearchAgent(
            model=aime_config.WEB_SEARCH_MODEL,
            tool_version=aime_config.WEB_SEARCH_TOOL_VERSION,
            record_api=_aime_usage.record_api,
        ) if aime_config.WEB_SEARCH_ENABLED else None
        backend = AnthropicMessagesBackend(
            system_prompt=aime_config.load_system_prompt(),
            model=aime_config.AGENT_MODEL,
            schema_files=aime_config.SCHEMA_FILES,
            conversations_dir=conv_dir,
            dek=dek,
            router=router,
            web_search_schema=(
                aime_config.WEB_SEARCH_SCHEMA if aime_config.WEB_SEARCH_ENABLED else None
            ),
            terminal_tool_schema=aime_config.ONBOARDING_TOOL_SCHEMA,
        )
        backend.new_session()

        gateway = ToolGateway(api_url=aime_config.API_URL)
        self._calendar_service = CalendarService(gateway)
        self._topic_service = TopicService(gateway)

        self._controller = ConversationController(
            backend=backend,
            tool_gateway=gateway,
            worker_spawner=self._spawn_stream_worker,
            web_search_agent=web_search_agent,
            messenger=messenger,
            message_recipient=messaging_contact,
        )

    def _spawn_stream_worker(self, fn) -> None:
        # Textual's exclusive worker cancels any running one of the same name —
        # which is what we want after a /reset or /load that retired the old
        # stream loop via session_terminated.
        self.run_worker(fn, thread=True, exclusive=True, name="agent-stream")

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with ContentSwitcher(initial="assistant", id="modes"):
            yield AssistantView(id="assistant")
            yield CalendarView(id="calendar")
            yield TopicView(id="topics")
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = "assistant"
        self._controller.subscribe(self._on_core_event)
        self._controller.start()
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

    # --- core-event bridge ---

    def _on_core_event(self, event: CoreEvent) -> None:
        """Subscriber called by ConversationController. Marshals onto the UI
        thread when the controller emitted from a worker thread."""
        if threading.get_ident() == self._main_tid:
            self._handle_core_event(event)
        else:
            self.call_from_thread(self._handle_core_event, event)

    def _handle_core_event(self, event: CoreEvent) -> None:
        log = self.query_one("#transcript", RichLog)
        kind = event.kind

        if kind == "user_message_shown":
            log.write(f"\n[bold cyan]you[/bold cyan]  {event.text}")
            if not event.from_replay:
                log.write("[dim]thinking…[/dim]")
                self._thinking_visible = True

        elif kind == "user_message_queued":
            log.write(
                f"\n[bold cyan]you[/bold cyan] [dim](queued — will send when "
                f"Aime is done)[/dim]  {event.text}"
            )

        elif kind == "assistant_text":
            self._clear_thinking()
            self._stream_flush_all(log)
            self._ensure_assistant_prefix(log)
            self._safe_write(log, event.text)

        elif kind == "assistant_text_delta":
            self._clear_thinking()
            self._ensure_assistant_prefix(log)
            self._stream_buffer += event.text
            self._stream_flush_lines(log)

        elif kind == "assistant_text_end":
            self._stream_flush_all(log)

        elif kind == "assistant_thinking":
            self._clear_thinking()
            log.write(f"[dim]{event.text}[/dim]")

        elif kind == "tool_call":
            self._clear_thinking()
            detail_str = (
                f" [dim italic]· {escape_markup(event.tool_details)}[/dim italic]"
                if event.tool_details else ""
            )
            log.write(
                f"[dim] Waiting on tool: [/dim][cyan]{event.tool_name}[/cyan]"
                f"[dim]…[/dim]{detail_str}"
            )

        elif kind == "tool_result":
            log.write(
                f"[dim] Tool result: [/dim][green]{event.tool_name}[/green]"
                f"[dim italic] · {escape_markup(event.tool_result_summary)}"
                f"[/dim italic]"
            )

        elif kind == "turn_end":
            self._stream_flush_all(log)
            if event.stop_reason == "end_turn":
                self._assistant_prefixed = False

        elif kind == "ready":
            log.write("[dim]_____________________[/dim]")
            log.write("[bold green]Aime ready[/bold green]")

        elif kind == "notice":
            color = _NOTICE_COLOR.get(event.severity, "white")
            log.write(f"[{color}] {event.text} [/{color}]")

        elif kind == "session_restart":
            log.clear()
            self._assistant_prefixed = False
            self._thinking_visible = False
            self._stream_buffer = ""

        elif kind == "session_terminated":
            log.write("[dim]This session has ended. See you next time![/dim]")

        elif kind == "error":
            log.write(f"[red]error:[/red] {event.text}")

    # --- transcript helpers ---

    def _clear_thinking(self) -> None:
        if self._thinking_visible:
            # RichLog can't retract a line; the "thinking…" stays in
            # scrollback. Just stop adding more.
            self._thinking_visible = False

    def _safe_write(self, log: RichLog, text: str) -> None:
        try:
            log.write(render_markup(text))
        except Exception:
            log.write(escape_markup(text))
            log.write(
                "[bold red] Pretty output is disabled. Model made small "
                "mistake in formatting response.[/bold red]"
            )

    def _ensure_assistant_prefix(self, log: RichLog) -> None:
        if not self._assistant_prefixed:
            log.write("[bold red]aime[/bold red]")
            self._assistant_prefixed = True

    def _stream_flush_lines(self, log: RichLog) -> None:
        """Flush any complete lines in _stream_buffer to the log. Partial
        trailing text stays buffered until the next delta or assistant_text_end."""
        if "\n" not in self._stream_buffer:
            return
        head, _, tail = self._stream_buffer.rpartition("\n")
        for line in head.split("\n"):
            self._safe_write(log, line)
        self._stream_buffer = tail

    def _stream_flush_all(self, log: RichLog) -> None:
        if self._stream_buffer:
            for line in self._stream_buffer.split("\n"):
                if line:
                    self._safe_write(log, line)
            self._stream_buffer = ""


if __name__ == "__main__":
    Aime().run()
