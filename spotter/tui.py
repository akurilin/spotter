"""Terminal UI for inspecting spotter run and alert history."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar

from textual.app import App, ComposeResult
from textual.widgets import DataTable, Footer, Header, Sparkline, Static, TabbedContent, TabPane

from spotter.errors import ConfigError

type JsonObject = dict[str, Any]

RUN_COLUMNS: tuple[str, ...] = (
    "Started",
    "Status",
    "Dry",
    "Raw",
    "Configured",
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
RUN_SPARKLINE_LIMIT = 50


class SpotterTui(App):
    """Textual application for read-only spotter log inspection."""

    BINDINGS: ClassVar[list[tuple[str, str, str]]] = [
        ("1", "show_runs", "1 Runs"),
        ("2", "show_alerts", "2 Alerts"),
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
    """

    def __init__(self, config: dict[str, Any]) -> None:
        """Initialize the TUI with configured read-only log paths."""
        super().__init__()
        self.title = "spotter"
        self.usage_path = optional_file_path(config, "usage")
        self.alerts_path = required_file_path(config, "alerts")

    def compose(self) -> ComposeResult:
        """Build the app layout with numbered top-level tabs."""
        yield Header()
        with TabbedContent(initial="runs", id="view-tabs"):
            with TabPane("1 Runs", id="runs"):
                yield Static(id="runs-summary")
                yield Sparkline(id="runs-sparkline")
                yield DataTable(id="runs-table", cursor_type="row")
            with TabPane("2 Alerts", id="alerts"):
                yield DataTable(id="alerts-table", cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        """Load both tables once the tabbed layout has mounted."""
        self.refresh_runs_table()
        self.refresh_alerts_table()
        self.focus_runs_table()

    def action_show_runs(self) -> None:
        """Switch to the run history tab."""
        self.query_one("#view-tabs", TabbedContent).active = "runs"
        self.call_after_refresh(self.focus_runs_table)

    def action_show_alerts(self) -> None:
        """Switch to the alert history tab."""
        self.query_one("#view-tabs", TabbedContent).active = "alerts"
        self.call_after_refresh(self.focus_alerts_table)

    def action_refresh_data(self) -> None:
        """Refresh the currently active table from disk."""
        active_tab = self.query_one("#view-tabs", TabbedContent).active
        if active_tab == "alerts":
            self.refresh_alerts_table()
            self.focus_alerts_table()
            return
        self.refresh_runs_table()
        self.focus_runs_table()

    def focus_runs_table(self) -> None:
        """Focus the run table and place its cursor on the first row."""
        focus_first_table_row(self.query_one("#runs-table", DataTable))

    def focus_alerts_table(self) -> None:
        """Focus the alert table and place its cursor on the first row."""
        focus_first_table_row(self.query_one("#alerts-table", DataTable))

    def refresh_runs_table(self) -> None:
        """Reload usage records from disk into the runs table."""
        table = self.query_one("#runs-table", DataTable)
        reset_table(table, RUN_COLUMNS)

        rows = read_jsonl_objects(self.usage_path)
        self.refresh_runs_sparkline(rows)
        if not rows:
            add_empty_row(table, len(RUN_COLUMNS), "No run records found.")
            return

        for row in reversed(rows):
            table.add_row(
                format_timestamp(row.get("started_at")),
                text_value(row.get("status")),
                format_bool(row.get("dry_run")),
                format_int(row.get("raw_messages")),
                format_int(row.get("configured_messages")),
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
        table = self.query_one("#alerts-table", DataTable)
        reset_table(table, ALERT_COLUMNS)

        rows = read_jsonl_objects(self.alerts_path)
        if not rows:
            add_empty_row(table, len(ALERT_COLUMNS), "No alert records found.")
            return

        for row in reversed(rows):
            table.add_row(
                format_timestamp(row.get("created_at")),
                shorten(row.get("topic_name"), 28),
                format_confidence(row.get("confidence")),
                shorten(row.get("group_name"), 36),
                shorten(row.get("sender_name"), 28),
                format_timestamp(row.get("local_time")),
                shorten(row.get("text"), 96),
            )


def run_tui(config: dict[str, Any]) -> int:
    """Start the terminal UI and block until the user exits it."""
    SpotterTui(config).run()
    return 0


def reset_table(table: DataTable, columns: tuple[str, ...]) -> None:
    """Clear a data table and replace its column headers."""
    table.clear(columns=True)
    table.add_columns(*columns)


def add_empty_row(table: DataTable, column_count: int, message: str) -> None:
    """Add a single placeholder row that fits the table's current column count."""
    table.add_row(message, *[""] * max(column_count - 1, 0))


def focus_first_table_row(table: DataTable) -> None:
    """Focus a data table and position keyboard navigation at its first row."""
    table.focus()
    table.move_cursor(row=0, column=0, animate=False, scroll=True)


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


def shorten(value: Any, max_chars: int) -> str:
    """Return a single-line string capped to the requested display length."""
    text = " ".join(text_value(value).split())
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
