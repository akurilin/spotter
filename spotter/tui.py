"""Terminal UI for spotter history, configuration, and LaunchAgent controls."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar

from textual import events
from textual.app import App, ComposeResult
from textual.widgets import DataTable, Footer, Header, Sparkline, Static, TabbedContent, TabPane

from spotter.errors import ConfigError
from spotter.launchagent import (
    LaunchAgentStatus,
    inspect_launch_agent,
    install_launch_agent,
    status_problem_details,
    uninstall_launch_agent,
)
from spotter.preflight import WhatsAppDatabaseAccess, check_whatsapp_database_access

type JsonObject = dict[str, Any]
type FileSignature = tuple[int, int] | None
type TableViewState = tuple[int, float, float]

RUN_COLUMNS: tuple[str, ...] = (
    "Started",
    "Status",
    "Dry",
    "Messages",
    "Batches",
    "Alerts",
    "Input",
    "Output",
    "Model",
)
ALERT_COLUMNS: tuple[str, ...] = (
    "Created",
    "Topic",
    "Confidence",
    "Group",
    "Sender",
    "Message Time",
    "Text",
)
AGENT_COLUMNS: tuple[str, ...] = (
    "Check",
    "Status",
    "Details",
)
CONFIG_COLUMNS: tuple[str, ...] = (
    "Setting",
    "Value",
)
TOPIC_COLUMNS: tuple[str, ...] = (
    "Priority",
    "ID",
    "Name",
    "Threshold",
    "Description",
)
TOPIC_COLUMN_WIDTHS: tuple[int, ...] = (8, 24, 28, 10, 80)
RUN_SPARKLINE_LIMIT = 50
HISTORY_REFRESH_INTERVAL_SECONDS = 3
REDACTED_CONFIG_VALUE = "[redacted]"


class SpotterTui(App):
    """Textual application for spotter history and LaunchAgent operations."""

    BINDINGS: ClassVar[list[tuple[str, str, str]]] = [
        ("1", "show_alerts", "1 Alerts"),
        ("2", "show_runs", "2 Runs"),
        ("3", "show_agent", "3 Agent"),
        ("4", "show_config", "4 Config"),
        ("5", "show_topics", "5 Topics"),
        ("f5", "refresh_data", "Refresh"),
        ("q", "quit", "Quit"),
    ]
    CSS: ClassVar[str] = """
    Screen, TabbedContent, TabPane {
        layout: vertical;
    }

    DataTable {
        height: 1fr;
    }

    #runs-summary {
        height: 1;
        padding: 0 1;
    }

    #runs-sparkline {
        height: 1;
        margin: 0 1 1 1;
    }

    #runs-shortcuts, #alerts-shortcuts, #config-summary, #topics-summary {
        height: 1;
        padding: 0 1;
        color: $text-muted;
    }

    #agent-status {
        height: 1;
        padding: 0 1;
        text-style: bold;
    }

    #agent-shortcuts {
        height: 1;
        padding: 0 1;
        color: $text-muted;
    }
    """

    def __init__(self, config: dict[str, Any], config_path: Path) -> None:
        """Initialize the TUI with configured read-only log paths."""
        super().__init__()
        self.config = config
        self.config_path = config_path
        self.title = "spotter"
        self.usage_path = optional_file_path(config, "usage")
        self.alerts_path = required_file_path(config, "alerts")
        self.runs_newest_first = True
        self.alerts_newest_first = True
        self.runs_file_signature: FileSignature = None
        self.alerts_file_signature: FileSignature = None

    def compose(self) -> ComposeResult:
        """Build the app layout with numbered top-level tabs."""
        yield Header()
        with TabbedContent(initial="alerts", id="view-tabs"):
            with TabPane("1 Alerts", id="alerts"):
                yield Static(id="alerts-shortcuts")
                yield DataTable(id="alerts-table", cursor_type="row")
            with TabPane("2 Runs", id="runs"):
                yield Static(id="runs-shortcuts")
                yield Static(id="runs-summary")
                yield Sparkline(id="runs-sparkline")
                yield DataTable(id="runs-table", cursor_type="row")
            with TabPane("3 Agent", id="agent"):
                yield Static(id="agent-status")
                yield Static(
                    "Keys: e enable automatic runs | d disable automatic runs | F5 refresh", id="agent-shortcuts"
                )
                yield DataTable(id="agent-table", cursor_type="row")
            with TabPane("4 Config", id="config"):
                yield Static(id="config-summary")
                yield DataTable(id="config-table", cursor_type="row")
            with TabPane("5 Topics", id="topics"):
                yield Static(id="topics-summary")
                yield DataTable(id="topics-table", cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        """Load both tables once the tabbed layout has mounted."""
        self.refresh_runs_table()
        self.refresh_alerts_table()
        self.refresh_agent_panel()
        self.refresh_config_table()
        self.refresh_topics_table()
        self.focus_alerts_table()
        self.set_interval(HISTORY_REFRESH_INTERVAL_SECONDS, self.refresh_changed_history)

    def action_show_runs(self) -> None:
        """Switch to the run history tab."""
        self.query_one("#view-tabs", TabbedContent).active = "runs"
        self.call_after_refresh(self.focus_runs_table)

    def action_show_alerts(self) -> None:
        """Switch to the alert history tab."""
        self.query_one("#view-tabs", TabbedContent).active = "alerts"
        self.call_after_refresh(self.focus_alerts_table)

    def action_show_agent(self) -> None:
        """Switch to the LaunchAgent status and controls tab."""
        self.query_one("#view-tabs", TabbedContent).active = "agent"
        self.call_after_refresh(self.focus_agent_table)

    def action_show_config(self) -> None:
        """Switch to the non-topic configuration tab."""
        self.query_one("#view-tabs", TabbedContent).active = "config"
        self.call_after_refresh(self.focus_config_table)

    def action_show_topics(self) -> None:
        """Switch to the configured topics tab."""
        self.query_one("#view-tabs", TabbedContent).active = "topics"
        self.call_after_refresh(self.focus_topics_table)

    def action_refresh_data(self) -> None:
        """Refresh the currently active view."""
        active_tab = self.query_one("#view-tabs", TabbedContent).active
        if active_tab == "alerts":
            self.refresh_alerts_table()
            self.focus_alerts_table()
            return
        if active_tab == "agent":
            self.refresh_agent_panel()
            self.focus_agent_table()
            return
        if active_tab in {"config", "topics"}:
            self.reload_config_views()
            if active_tab == "config":
                self.focus_config_table()
            else:
                self.focus_topics_table()
            return
        self.refresh_runs_table()
        self.focus_runs_table()

    def on_key(self, event: events.Key) -> None:
        """Handle key commands that are local to the active tab."""
        active_tab = self.query_one("#view-tabs", TabbedContent).active
        if active_tab == "runs" and event.key == "s":
            event.stop()
            self.runs_newest_first = not self.runs_newest_first
            self.refresh_runs_table()
            self.focus_runs_table()
            return
        if active_tab == "alerts" and event.key == "s":
            event.stop()
            self.alerts_newest_first = not self.alerts_newest_first
            self.refresh_alerts_table()
            self.focus_alerts_table()
            return
        if active_tab != "agent":
            return
        if event.key == "e":
            event.stop()
            self.enable_launch_agent()
            return
        if event.key == "d":
            event.stop()
            self.disable_launch_agent()

    def focus_runs_table(self) -> None:
        """Focus the run table and place its cursor on the first row."""
        focus_first_table_row(self.query_one("#runs-table", DataTable))

    def focus_alerts_table(self) -> None:
        """Focus the alert table and place its cursor on the first row."""
        focus_first_table_row(self.query_one("#alerts-table", DataTable))

    def focus_agent_table(self) -> None:
        """Focus the agent status table and place its cursor on the first row."""
        focus_first_table_row(self.query_one("#agent-table", DataTable))

    def focus_config_table(self) -> None:
        """Focus the configuration table and place its cursor on the first row."""
        focus_first_table_row(self.query_one("#config-table", DataTable))

    def focus_topics_table(self) -> None:
        """Focus the topics table and place its cursor on the first row."""
        focus_first_table_row(self.query_one("#topics-table", DataTable))

    def refresh_changed_history(self) -> None:
        """Reload history widgets only when their source files have changed."""
        runs_changed = file_signature(self.usage_path) != self.runs_file_signature
        alerts_changed = file_signature(self.alerts_path) != self.alerts_file_signature
        if not runs_changed and not alerts_changed:
            return

        runs_view = capture_table_view(self.query_one("#runs-table", DataTable)) if runs_changed else None
        alerts_view = capture_table_view(self.query_one("#alerts-table", DataTable)) if alerts_changed else None
        with self.batch_update():
            if runs_changed:
                self.refresh_runs_table()
                restore_table_view(self.query_one("#runs-table", DataTable), runs_view)
            if alerts_changed:
                self.refresh_alerts_table()
                restore_table_view(self.query_one("#alerts-table", DataTable), alerts_view)

    def refresh_runs_table(self) -> None:
        """Reload usage records from disk into the runs table."""
        self.runs_file_signature = file_signature(self.usage_path)
        table = self.query_one("#runs-table", DataTable)
        reset_table(table, RUN_COLUMNS)
        self.query_one("#runs-shortcuts", Static).update(format_sort_shortcuts(self.runs_newest_first))

        rows = read_jsonl_objects(self.usage_path)
        self.refresh_runs_sparkline(rows)
        if not rows:
            add_empty_row(table, len(RUN_COLUMNS), "No run records found.")
            return

        for row in sort_rows_by_timestamp(rows, "started_at", self.runs_newest_first):
            table.add_row(
                format_timestamp(row.get("started_at")),
                text_value(row.get("status")),
                format_bool(row.get("dry_run")),
                format_int(row.get("messages")),
                format_int(row.get("batches")),
                format_int(row.get("alerts")),
                format_int(row.get("input_tokens")),
                format_int(row.get("output_tokens")),
                shorten(row.get("model"), 32),
            )

    def refresh_runs_sparkline(self, rows: list[JsonObject]) -> None:
        """Update the alerts-per-run sparkline and summary from usage records."""
        summary = self.query_one("#runs-summary", Static)
        sparkline = self.query_one("#runs-sparkline", Sparkline)
        alert_counts = alert_counts_by_run(rows)
        sparkline.data = alert_counts[-RUN_SPARKLINE_LIMIT:]
        summary.update(format_runs_summary(alert_counts))

    def refresh_alerts_table(self) -> None:
        """Reload alert records from disk into the alerts table."""
        self.alerts_file_signature = file_signature(self.alerts_path)
        table = self.query_one("#alerts-table", DataTable)
        reset_table(table, ALERT_COLUMNS)
        self.query_one("#alerts-shortcuts", Static).update(format_sort_shortcuts(self.alerts_newest_first))

        rows = read_jsonl_objects(self.alerts_path)
        if not rows:
            add_empty_row(table, len(ALERT_COLUMNS), "No alert records found.")
            return

        for row in sort_rows_by_timestamp(rows, "created_at", self.alerts_newest_first):
            table.add_row(
                format_timestamp(row.get("created_at")),
                shorten(row.get("topic_name"), 28),
                format_confidence(row.get("confidence")),
                shorten(row.get("group_name"), 36),
                shorten(row.get("sender_name"), 28),
                format_timestamp(row.get("local_time")),
                shorten(row.get("text"), 96),
            )

    def refresh_agent_panel(self, message: str | None = None) -> None:
        """Reload LaunchAgent and database-access status into the Agent tab."""
        table = self.query_one("#agent-table", DataTable)
        reset_table(table, AGENT_COLUMNS)

        try:
            status = inspect_launch_agent(self.config, self.config_path)
            database_access = check_whatsapp_database_access(self.config)
        except (ConfigError, OSError, RuntimeError) as exc:
            self.query_one("#agent-status", Static).update(message or f"Automatic runs: UNKNOWN | {exc}")
            table.add_row("Status", "error", str(exc))
            return

        self.query_one("#agent-status", Static).update(format_agent_summary(status, database_access, message))

        for check, status_text, details in agent_status_rows(status, database_access):
            table.add_row(check, status_text, details)

    def reload_config_views(self) -> None:
        """Reload config.json and update both configuration views atomically."""
        try:
            config = read_config_object(self.config_path)
            usage_path = optional_file_path(config, "usage")
            alerts_path = required_file_path(config, "alerts")
        except (ConfigError, OSError) as exc:
            message = f"Reload failed: {exc}"
            self.query_one("#config-summary", Static).update(message)
            self.query_one("#topics-summary", Static).update(message)
            return

        self.config = config
        self.usage_path = usage_path
        self.alerts_path = alerts_path
        with self.batch_update():
            self.refresh_config_table("Reloaded")
            self.refresh_topics_table("Reloaded")

    def refresh_config_table(self, message: str | None = None) -> None:
        """Render loaded non-topic configuration values."""
        table = self.query_one("#config-table", DataTable)
        reset_table(table, CONFIG_COLUMNS)
        rows = config_display_rows(self.config)
        summary = f"Loaded config: {self.config_path} | {len(rows)} settings"
        self.query_one("#config-summary", Static).update(f"{summary} | {message}" if message else summary)
        if not rows:
            add_empty_row(table, len(CONFIG_COLUMNS), "No non-topic configuration found.")
            return
        for setting, value in rows:
            table.add_row(setting, value)

    def refresh_topics_table(self, message: str | None = None) -> None:
        """Render configured topics in classifier priority order."""
        table = self.query_one("#topics-table", DataTable)
        reset_table_with_widths(table, TOPIC_COLUMNS, TOPIC_COLUMN_WIDTHS)
        rows = topic_display_rows(self.config)
        summary = f"Configured topics: {len(rows)} | First match has priority"
        self.query_one("#topics-summary", Static).update(f"{summary} | {message}" if message else summary)
        if not rows:
            add_empty_row(table, len(TOPIC_COLUMNS), "No topics configured.")
            return
        for row in rows:
            table.add_row(*row, height=None)

    def enable_launch_agent(self) -> None:
        """Install or update the LaunchAgent from the current config."""
        try:
            install_launch_agent(self.config, self.config_path, emit=False)
        except (ConfigError, OSError, RuntimeError, subprocess.CalledProcessError) as exc:
            self.refresh_agent_panel(f"Enable failed: {exc}")
            self.focus_agent_table()
            return
        self.refresh_agent_panel("Automatic runs enabled.")
        self.focus_agent_table()

    def disable_launch_agent(self) -> None:
        """Unload and remove the LaunchAgent."""
        try:
            uninstall_launch_agent(self.config, emit=False)
        except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
            self.refresh_agent_panel(f"Disable failed: {exc}")
            self.focus_agent_table()
            return
        self.refresh_agent_panel("Automatic runs disabled.")
        self.focus_agent_table()


def run_tui(config: dict[str, Any], config_path: Path) -> int:
    """Start the terminal UI and block until the user exits it."""
    SpotterTui(config, config_path).run()
    return 0


def reset_table(table: DataTable, columns: tuple[str, ...]) -> None:
    """Clear a data table and replace its column headers."""
    table.clear(columns=True)
    table.add_columns(*columns)


def reset_table_with_widths(table: DataTable, columns: tuple[str, ...], widths: tuple[int, ...]) -> None:
    """Clear a data table and replace its column headers with fixed widths."""
    table.clear(columns=True)
    for column, width in zip(columns, widths, strict=True):
        table.add_column(column, width=width)


def add_empty_row(table: DataTable, column_count: int, message: str) -> None:
    """Add a single placeholder row that fits the table's current column count."""
    table.add_row(message, *[""] * max(column_count - 1, 0))


def focus_first_table_row(table: DataTable) -> None:
    """Focus a data table and position keyboard navigation at its first row."""
    table.focus()
    table.move_cursor(row=0, column=0, animate=False, scroll=True)


def capture_table_view(table: DataTable) -> TableViewState:
    """Capture cursor and scroll positions before an automatic table update."""
    return table.cursor_row, table.scroll_x, table.scroll_y


def restore_table_view(table: DataTable, view: TableViewState | None) -> None:
    """Restore cursor and scroll positions without changing keyboard focus."""
    if view is None:
        return
    cursor_row, scroll_x, scroll_y = view
    table.move_cursor(row=min(cursor_row, max(table.row_count - 1, 0)), animate=False, scroll=False)
    table.scroll_to(x=scroll_x, y=scroll_y, animate=False, force=True)


def read_jsonl_objects(path: Path | None) -> list[JsonObject]:
    """Read JSON object rows from a JSON Lines file, skipping malformed rows."""
    if path is None or not path.exists():
        return []

    rows: list[JsonObject] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def read_config_object(path: Path) -> dict[str, Any]:
    """Read and validate the configuration needed by the TUI."""
    try:
        with path.open("r", encoding="utf-8") as handle:
            config = json.load(handle)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Invalid JSON in {path}: {exc.msg}") from exc

    if not isinstance(config, dict):
        raise ConfigError("Config root must be an object.")
    validate_topics_config(config)
    return config


def validate_topics_config(config: dict[str, Any]) -> None:
    """Validate configured topic structure before replacing the active config."""
    topics = config.get("topics")
    if not isinstance(topics, list) or not topics:
        raise ConfigError("Config must contain at least one topic.")

    topic_ids: list[str] = []
    for topic in topics:
        if not isinstance(topic, dict):
            raise ConfigError("Each topic must be an object.")
        for key in ("id", "name", "description"):
            value = topic.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ConfigError(f"Expected non-empty string for topic.{key}")
        try:
            float(topic.get("threshold", 0.75))
        except (TypeError, ValueError) as exc:
            raise ConfigError("Expected numeric topic.threshold") from exc
        topic_ids.append(topic["id"].strip())

    duplicates = sorted({topic_id for topic_id in topic_ids if topic_ids.count(topic_id) > 1})
    if duplicates:
        raise ConfigError(f"Duplicate topic ids: {', '.join(duplicates)}")


def file_signature(path: Path | None) -> FileSignature:
    """Return metadata sufficient to detect ordinary file appends and replacements."""
    if path is None:
        return None
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size


def config_display_rows(config: dict[str, Any]) -> list[tuple[str, str]]:
    """Flatten loaded configuration for display, excluding topics and secrets."""
    rows: list[tuple[str, str]] = []
    for key, value in config.items():
        if key == "topics":
            continue
        rows.extend(flatten_config_value(key, value))
    return rows


def flatten_config_value(path: str, value: Any) -> list[tuple[str, str]]:
    """Flatten one configuration value into dotted display rows."""
    if is_sensitive_config_path(path):
        return [(path, REDACTED_CONFIG_VALUE)]
    if isinstance(value, dict):
        if not value:
            return [(path, "{}")]
        rows: list[tuple[str, str]] = []
        for key, nested_value in value.items():
            rows.extend(flatten_config_value(f"{path}.{key}", nested_value))
        return rows
    if isinstance(value, list):
        if not value:
            return [(path, "[]")]
        rows = []
        for index, nested_value in enumerate(value):
            rows.extend(flatten_config_value(f"{path}[{index}]", nested_value))
        return rows
    return [(path, format_config_value(value))]


def is_sensitive_config_path(path: str) -> bool:
    """Return whether a configuration path likely contains a secret."""
    key = path.rsplit(".", maxsplit=1)[-1].split("[", maxsplit=1)[0].lower().replace("-", "_")
    sensitive_names = {
        "api_key",
        "apikey",
        "app_token",
        "credentials",
        "key",
        "password",
        "secret",
        "secrets",
        "token",
        "user_key",
    }
    return key in sensitive_names or key.endswith(("_api_key", "_password", "_secret", "_token"))


def format_config_value(value: Any) -> str:
    """Format a scalar configuration value for compact display."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    return single_line_text(value)


def topic_display_rows(config: dict[str, Any]) -> list[tuple[str, str, str, str, str]]:
    """Return configured topics in classifier priority order."""
    topics = config.get("topics", [])
    if not isinstance(topics, list):
        return []

    rows = []
    for priority, topic in enumerate(topics, start=1):
        if not isinstance(topic, dict):
            continue
        rows.append(
            (
                str(priority),
                single_line_text(topic.get("id")),
                single_line_text(topic.get("name")),
                format_confidence(topic.get("threshold", 0.75)),
                single_line_text(topic.get("description")),
            )
        )
    return rows


def sort_rows_by_timestamp(rows: list[JsonObject], field: str, newest_first: bool) -> list[JsonObject]:
    """Sort dated rows in the requested direction, leaving undated rows last."""
    dated_rows: list[tuple[datetime, JsonObject]] = []
    undated_rows: list[JsonObject] = []
    for row in rows:
        timestamp = parse_timestamp(row.get(field))
        if timestamp is None:
            undated_rows.append(row)
        else:
            dated_rows.append((timestamp, row))

    dated_rows.sort(key=lambda item: item[0], reverse=newest_first)
    return [row for _, row in dated_rows] + undated_rows


def parse_timestamp(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp into a consistently comparable datetime."""
    text = text_value(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.astimezone()


def format_sort_shortcuts(newest_first: bool) -> str:
    """Return the page-local date-order status and shortcuts."""
    order = "newest first" if newest_first else "oldest first"
    return (
        f"Date order: {order} | Auto-refresh: {HISTORY_REFRESH_INTERVAL_SECONDS}s | "
        "Keys: s reverse date order | F5 refresh"
    )


def optional_file_path(config: dict[str, Any], key: str) -> Path | None:
    """Resolve an optional file path from the config's files section."""
    files = config.get("files", {})
    if not isinstance(files, dict):
        raise ConfigError("files config must be an object.")

    value = files.get(key)
    if not isinstance(value, str) or not value.strip():
        return None
    return Path(value).expanduser()


def required_file_path(config: dict[str, Any], key: str) -> Path:
    """Resolve a required file path from the config's files section."""
    path = optional_file_path(config, key)
    if path is None:
        raise ConfigError(f"Missing files.{key} in config.")
    return path


def text_value(value: Any) -> str:
    """Return a display-safe string for a scalar JSON value."""
    if value is None:
        return ""
    return str(value)


def single_line_text(value: Any) -> str:
    """Return display-safe text without line breaks or repeated whitespace."""
    return " ".join(text_value(value).split())


def shorten(value: Any, max_chars: int) -> str:
    """Return a single-line string capped to the requested display length."""
    text = single_line_text(value)
    if len(text) <= max_chars:
        return text
    return f"{text[: max(max_chars - 3, 0)].rstrip()}..."


def format_bool(value: Any) -> str:
    """Format a JSON boolean for table display."""
    if isinstance(value, bool):
        return "yes" if value else "no"
    return text_value(value)


def format_int(value: Any) -> str:
    """Format an integer-like JSON value with thousands separators."""
    if isinstance(value, bool) or value is None:
        return ""
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return text_value(value)


def int_value(value: Any) -> int:
    """Parse an integer-like JSON value, defaulting malformed values to zero."""
    if isinstance(value, bool) or value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def alert_counts_by_run(rows: list[JsonObject]) -> list[float]:
    """Return alert counts in chronological run order for sparkline rendering."""
    return [float(int_value(row.get("alerts"))) for row in rows]


def format_runs_summary(alert_counts: list[float]) -> str:
    """Format aggregate alert statistics for the runs sparkline."""
    if not alert_counts:
        return "Alerts per run: no run records"

    displayed_counts = alert_counts[-RUN_SPARKLINE_LIMIT:]
    total_alerts = int(sum(alert_counts))
    displayed_total = int(sum(displayed_counts))
    displayed_average = displayed_total / len(displayed_counts)
    displayed_max = int(max(displayed_counts))
    return (
        f"Alerts per run: last {len(displayed_counts)} runs | "
        f"total {total_alerts:,} | avg {displayed_average:.1f} | max {displayed_max:,}"
    )


def format_agent_summary(
    status: LaunchAgentStatus, database_access: WhatsAppDatabaseAccess, message: str | None = None
) -> str:
    """Return an at-a-glance LaunchAgent status line for the Agent tab."""
    if status.is_configured_correctly:
        agent_state = "ENABLED"
    elif status.plist_exists or status.loaded:
        agent_state = "NEEDS ATTENTION"
    else:
        agent_state = "DISABLED"

    database_state = "OK" if database_access.ok else "BLOCKED"
    summary = f"Automatic runs: {agent_state} | WhatsApp DB access: {database_state}"
    if message:
        return f"{summary} | {message}"
    return summary


def agent_status_rows(status: LaunchAgentStatus, database_access: WhatsAppDatabaseAccess) -> list[tuple[str, str, str]]:
    """Build display rows for LaunchAgent and database-access status."""
    rows = [
        (
            "Configured correctly",
            yes_no(status.is_configured_correctly),
            "Loaded in launchd and installed plist matches the current config.",
        ),
        ("Loaded", yes_no(status.loaded), status.loaded_error or status.service_name),
        ("Plist installed", yes_no(status.plist_exists), str(status.plist_path)),
        ("Plist loadable", yes_no(status.plist_loadable), format_plist_load_detail(status)),
        ("Matches current config", status_label(status.plist_matches_config), format_plist_match_detail(status)),
        ("Can install from current Python", yes_no(status.can_install), status.expected_plist_error or "Ready."),
        ("Current Python", "path", str(status.current_python_path)),
        ("Installed Python", "path", status.installed_python_path or "Not installed."),
        ("Installed config", "path", status.installed_config_path or "Not installed."),
        ("App log", "path", str(status.app_log_path)),
        ("WhatsApp DB access", yes_no(database_access.ok), database_access.detail),
        ("WhatsApp DB path", "path", str(database_access.db_path or "unknown")),
    ]

    for detail in status_problem_details(status):
        rows.append(("Attention", "!", detail))
    return rows


def format_plist_load_detail(status: LaunchAgentStatus) -> str:
    """Return a clear plist loadability detail for the Agent tab."""
    if not status.plist_exists:
        return "No plist installed."
    return status.plist_error or "Valid property list."


def format_plist_match_detail(status: LaunchAgentStatus) -> str:
    """Return a clear plist/config comparison detail for the Agent tab."""
    if not status.plist_exists:
        return "Enable automatic runs to install the generated plist."
    if status.plist_matches_config is None:
        return "Cannot compare until the plist and expected config are both readable."
    if status.plist_matches_config:
        return "Installed plist matches the current config."
    return "Re-enable automatic runs to update the installed plist."


def yes_no(value: bool) -> str:
    """Format a boolean for compact table display."""
    return "yes" if value else "no"


def status_label(value: bool | None) -> str:
    """Format a tri-state status for compact table display."""
    if value is None:
        return "unknown"
    return yes_no(value)


def format_confidence(value: Any) -> str:
    """Format a confidence score as a percentage when possible."""
    if value is None:
        return ""
    try:
        return f"{float(value) * 100:.0f}%"
    except (TypeError, ValueError):
        return text_value(value)


def format_timestamp(value: Any) -> str:
    """Format an ISO-8601 timestamp for compact table display."""
    text = text_value(value)
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return shorten(text, 19)
    return parsed.strftime("%Y-%m-%d %H:%M:%S")
