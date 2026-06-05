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


if __name__ == "__main__":
    unittest.main()
