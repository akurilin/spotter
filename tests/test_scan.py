from __future__ import annotations

import io
import json
import logging
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from spotter.models import ClassificationResult, Match, Message
from spotter.usage import UsageAccumulator
from spotter.whatsapp_db import FetchResult
from tests.support import TestCase, config_dict, load_spotter_cli, make_config

spotter_cli = load_spotter_cli()


class ScanTests(TestCase):
    def test_successful_scan_writes_expected_files(self):
        temp_dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        state_path = temp_dir / "state.json"
        alerts_path = temp_dir / "alerts.jsonl"
        errors_path = temp_dir / "errors.jsonl"
        usage_path = temp_dir / "usage.jsonl"
        log_path = temp_dir / "spotter.log"
        state_path.write_text(json.dumps({"last_processed_message_pk": 41}), encoding="utf-8")

        raw_config = config_dict(temp_dir)
        raw_config["topics"] = [
            {
                "id": "engineering_hiring",
                "name": "Engineering hiring",
                "description": "Engineering hiring advice",
                "threshold": 0.75,
            }
        ]
        config = make_config(temp_dir, raw_config)
        message = Message(
            message_pk=42,
            group_name="Founders Community",
            group_jid="12345-67890@g.us",
            sender_name="Founder",
            sender_jid="123456789@lid",
            local_time="2026-01-02 03:04:05",
            text="What is a good process for hiring an engineering leader?",
        )
        matches = (
            Match(
                message_pk=message.message_pk,
                topic_id="engineering_hiring",
                confidence=0.9,
                reason="Founder asks for engineering hiring advice.",
                notification="Founder seeking engineering hiring advice.",
            ),
        )
        database = MagicMock()

        def classify_messages(_llm_config, _batch_size, _topics, _messages):
            accumulator = UsageAccumulator()
            accumulator.add(
                SimpleNamespace(
                    input_tokens=100,
                    output_tokens=20,
                    cache_creation_input_tokens=10,
                    cache_read_input_tokens=5,
                )
            )
            return ClassificationResult(matches=matches, usage=accumulator)

        def close_log_handlers():
            for handler in list(spotter_cli.LOGGER.handlers):
                spotter_cli.LOGGER.removeHandler(handler)
                handler.close()

        logging.disable(logging.NOTSET)
        self.addCleanup(close_log_handlers)
        with patch.object(spotter_cli.sys, "stdout", io.StringIO()):
            spotter_cli.configure_logging(config.logging)

        with (
            patch.object(spotter_cli, "open_whatsapp_db", return_value=database),
            patch.object(spotter_cli, "fetch_message_local_time", return_value="2026-01-02 03:00:00"),
            patch.object(spotter_cli, "count_groups", return_value=1),
            patch.object(
                spotter_cli,
                "fetch_candidate_messages",
                return_value=FetchResult(messages=[message], fetched_high_water_pk=message.message_pk),
            ),
            patch.object(spotter_cli, "fetch_max_group_message_pk", return_value=message.message_pk),
            patch.object(spotter_cli, "classify_messages", side_effect=classify_messages) as classify_messages_mock,
            patch.object(spotter_cli, "notify_alerts", return_value=[]) as notify_alerts,
            patch.object(spotter_cli, "new_run_id", return_value="test-run"),
            patch.object(spotter_cli, "now_iso", return_value="2026-01-02T03:05:00+00:00"),
        ):
            result = spotter_cli.run_scan(config, dry_run=False, limit_override=None)

        self.assertEqual(0, result)
        database.__enter__.assert_called_once()
        database.__exit__.assert_called_once()
        classify_messages_mock.assert_called_once()

        alerts = read_jsonl(alerts_path)
        self.assertEqual(1, len(alerts))
        self.assertEqual(message.message_pk, alerts[0]["message_pk"])
        self.assertEqual("engineering_hiring", alerts[0]["topic_id"])
        self.assertEqual("2026-01-02T03:05:00+00:00", alerts[0]["created_at"])
        notify_alerts.assert_called_once()
        self.assertEqual(config.notifications, notify_alerts.call_args.args[0])
        self.assertEqual(alerts, [alert.to_dict() for alert in notify_alerts.call_args.args[1]])

        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(message.message_pk, state["last_processed_message_pk"])
        self.assertEqual("2026-01-02T03:05:00+00:00", state["last_run_at"])

        usage = read_jsonl(usage_path)
        self.assertEqual(1, len(usage))
        self.assertEqual("test-run", usage[0]["run_id"])
        self.assertEqual("ok", usage[0]["status"])
        self.assertEqual(1, usage[0]["messages"])
        self.assertEqual(1, usage[0]["alerts"])
        self.assertEqual(1, usage[0]["batches"])
        self.assertEqual(100, usage[0]["input_tokens"])
        self.assertEqual(20, usage[0]["output_tokens"])

        self.assertFalse(errors_path.exists())
        self.assertEqual(
            {"alerts.jsonl", "spotter.log", "state.json", "usage.jsonl"},
            {path.name for path in temp_dir.iterdir()},
        )
        log_text = log_path.read_text(encoding="utf-8")
        self.assertIn("Scanner run starting", log_text)
        self.assertIn("Classification complete: matches=1 alerts_after_thresholds=1", log_text)
        self.assertIn("Wrote 1 alert(s).", log_text)
        self.assertIn("Advanced cursor to message 42.", log_text)


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
