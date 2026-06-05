from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from textual.widgets import DataTable, Static, TabbedContent

from spotter.tui import SpotterTui, sort_rows_by_timestamp


class TuiTests(unittest.IsolatedAsyncioTestCase):
    async def test_alerts_are_first_and_history_pages_reverse_date_order_with_s(self):
        temp_dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        alerts_path = temp_dir / "alerts.jsonl"
        usage_path = temp_dir / "usage.jsonl"
        write_jsonl(
            alerts_path,
            [
                {"created_at": "2026-03-01T00:00:00+00:00", "topic_name": "Newest alert"},
                {"created_at": "2026-01-01T00:00:00+00:00", "topic_name": "Oldest alert"},
            ],
        )
        write_jsonl(
            usage_path,
            [
                {"started_at": "2026-01-01T00:00:00+00:00", "status": "oldest"},
                {"started_at": "2026-02-01T00:00:00+00:00", "status": "newest"},
            ],
        )
        app = SpotterTui(
            {"files": {"alerts": str(alerts_path), "usage": str(usage_path)}},
            temp_dir / "config.json",
        )

        with patch.object(app, "refresh_agent_panel"):
            async with app.run_test() as pilot:
                tabs = app.query_one("#view-tabs", TabbedContent)
                alerts_table = app.query_one("#alerts-table", DataTable)
                runs_table = app.query_one("#runs-table", DataTable)

                self.assertEqual("alerts", tabs.active)
                self.assertEqual("Newest alert", alerts_table.get_row_at(0)[1])
                self.assertIn("Date order: newest first", str(app.query_one("#alerts-shortcuts", Static).render()))
                self.assertIn("Auto-refresh: 3s", str(app.query_one("#alerts-shortcuts", Static).render()))

                await pilot.press("s")
                self.assertEqual("Oldest alert", alerts_table.get_row_at(0)[1])
                self.assertIn("Date order: oldest first", str(app.query_one("#alerts-shortcuts", Static).render()))

                await pilot.press("2")
                self.assertEqual("runs", tabs.active)
                self.assertEqual("newest", runs_table.get_row_at(0)[1])

                await pilot.press("s")
                self.assertEqual("oldest", runs_table.get_row_at(0)[1])
                self.assertIn("Date order: oldest first", str(app.query_one("#runs-shortcuts", Static).render()))

                await pilot.press("1")
                self.assertEqual("alerts", tabs.active)
                self.assertEqual("Oldest alert", alerts_table.get_row_at(0)[1])

    async def test_history_auto_refreshes_only_when_source_files_change(self):
        temp_dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        alerts_path = temp_dir / "alerts.jsonl"
        usage_path = temp_dir / "usage.jsonl"
        alerts = [
            {"created_at": "2026-01-01T00:00:00+00:00", "topic_name": "Initial alert"},
            {"created_at": "2025-12-01T00:00:00+00:00", "topic_name": "Older alert"},
        ]
        runs = [{"started_at": "2026-01-01T00:00:00+00:00", "status": "initial"}]
        write_jsonl(alerts_path, alerts)
        write_jsonl(usage_path, runs)
        app = SpotterTui(
            {"files": {"alerts": str(alerts_path), "usage": str(usage_path)}},
            temp_dir / "config.json",
        )

        with patch.object(app, "refresh_agent_panel"):
            async with app.run_test():
                alerts_table = app.query_one("#alerts-table", DataTable)
                runs_table = app.query_one("#runs-table", DataTable)
                alerts_table.move_cursor(row=1, animate=False)
                with (
                    patch.object(app, "refresh_alerts_table", wraps=app.refresh_alerts_table) as refresh_alerts,
                    patch.object(app, "refresh_runs_table", wraps=app.refresh_runs_table) as refresh_runs,
                ):
                    app.refresh_changed_history()
                    refresh_alerts.assert_not_called()
                    refresh_runs.assert_not_called()

                    alerts.append({"created_at": "2026-02-01T00:00:00+00:00", "topic_name": "New alert"})
                    write_jsonl(alerts_path, alerts)
                    app.refresh_changed_history()
                    refresh_alerts.assert_called_once()
                    refresh_runs.assert_not_called()
                    self.assertEqual("New alert", alerts_table.get_row_at(0)[1])
                    self.assertEqual(1, alerts_table.cursor_row)
                    self.assertIs(alerts_table, app.focused)

                    runs.append({"started_at": "2026-02-01T00:00:00+00:00", "status": "new"})
                    write_jsonl(usage_path, runs)
                    app.refresh_changed_history()
                    refresh_runs.assert_called_once()
                    self.assertEqual("new", runs_table.get_row_at(0)[1])

    async def test_config_and_topics_pages_render_separate_configuration_views(self):
        temp_dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        alerts_path = temp_dir / "alerts.jsonl"
        usage_path = temp_dir / "usage.jsonl"
        config_path = temp_dir / "config.json"
        config = {
            "files": {"alerts": str(alerts_path), "usage": str(usage_path)},
            "notifications": {"macos": True},
            "service": {"api_token": "do-not-display"},
            "topics": [
                {
                    "id": "priority_topic",
                    "name": "Priority topic",
                    "threshold": 0.8,
                    "description": "First configured topic.",
                },
                {
                    "id": "secondary_topic",
                    "name": "Secondary topic",
                    "threshold": 0.7,
                    "description": (
                        "Second topic with enough detail to wrap across multiple lines in the default topics table "
                        "description column while remaining completely readable."
                    ),
                },
            ],
        }
        write_json(config_path, config)
        app = SpotterTui(config, config_path)

        with patch.object(app, "refresh_agent_panel"):
            async with app.run_test() as pilot:
                tabs = app.query_one("#view-tabs", TabbedContent)

                await pilot.press("4")
                config_table = app.query_one("#config-table", DataTable)
                config_rows = {
                    config_table.get_row_at(index)[0]: config_table.get_row_at(index)[1]
                    for index in range(config_table.row_count)
                }
                self.assertEqual("config", tabs.active)
                self.assertEqual("true", config_rows["notifications.macos"])
                self.assertEqual("[redacted]", config_rows["service.api_token"])
                self.assertFalse(any(setting.startswith("topics") for setting in config_rows))
                self.assertIs(config_table, app.focused)

                await pilot.press("5")
                topics_table = app.query_one("#topics-table", DataTable)
                self.assertEqual("topics", tabs.active)
                self.assertEqual(
                    ["1", "priority_topic", "Priority topic", "80%", "First configured topic."],
                    topics_table.get_row_at(0),
                )
                self.assertEqual(
                    [
                        "2",
                        "secondary_topic",
                        "Secondary topic",
                        "70%",
                        (
                            "Second topic with enough detail to wrap across multiple lines in the default topics "
                            "table description column while remaining completely readable."
                        ),
                    ],
                    topics_table.get_row_at(1),
                )
                second_topic_key = list(topics_table.rows)[1]
                self.assertGreater(topics_table.get_row_height(second_topic_key), 1)
                self.assertIs(topics_table, app.focused)

                updated_config = {
                    **config,
                    "notifications": {"macos": False},
                    "topics": [
                        {
                            "id": "reloaded_topic",
                            "name": "Reloaded topic",
                            "threshold": 0.9,
                            "description": "Loaded from the edited config file.",
                        }
                    ],
                }
                write_json(config_path, updated_config)
                await pilot.press("f5")
                self.assertEqual(
                    ["1", "reloaded_topic", "Reloaded topic", "90%", "Loaded from the edited config file."],
                    topics_table.get_row_at(0),
                )
                self.assertIn("Reloaded", str(app.query_one("#topics-summary", Static).render()))

                await pilot.press("4")
                config_rows = {
                    config_table.get_row_at(index)[0]: config_table.get_row_at(index)[1]
                    for index in range(config_table.row_count)
                }
                self.assertEqual("false", config_rows["notifications.macos"])

                config_path.write_text("{invalid", encoding="utf-8")
                await pilot.press("f5")
                config_rows_after_failed_reload = {
                    config_table.get_row_at(index)[0]: config_table.get_row_at(index)[1]
                    for index in range(config_table.row_count)
                }
                self.assertEqual("false", config_rows_after_failed_reload["notifications.macos"])
                self.assertIn("Reload failed:", str(app.query_one("#config-summary", Static).render()))

    def test_sort_rows_by_timestamp_leaves_malformed_dates_last(self):
        rows = [
            {"created_at": "not-a-date", "id": "malformed"},
            {"created_at": "2026-01-01T01:00:00+01:00", "id": "same-time-first"},
            {"created_at": "2026-01-01T00:00:00+00:00", "id": "same-time-second"},
        ]

        sorted_rows = sort_rows_by_timestamp(rows, "created_at", newest_first=False)

        self.assertEqual(["same-time-first", "same-time-second", "malformed"], [row["id"] for row in sorted_rows])


def write_jsonl(path: Path, rows: list[dict[str, str]]) -> None:
    path.write_text("".join(f"{json.dumps(row)}\n" for row in rows), encoding="utf-8")


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
