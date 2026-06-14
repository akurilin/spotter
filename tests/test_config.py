from __future__ import annotations

from pathlib import Path

from spotter.config import DEFAULT_MODEL, parse_config
from spotter.errors import ConfigError
from tests.support import TestCase, config_dict


class ConfigTests(TestCase):
    def test_parse_config_applies_defaults_and_returns_typed_sections(self):
        raw = config_dict(self.temp_dir)
        raw.pop("logging")
        raw.pop("notifications")

        config = parse_config(raw)

        self.assertEqual(DEFAULT_MODEL, config.llm.model)
        self.assertEqual(200, config.whatsapp.batch_size)
        self.assertTrue(config.notifications.macos)
        self.assertEqual("INFO", config.logging.level)
        self.assertEqual("example_topic", config.topics[0].id)

    def test_parse_config_rejects_duplicate_topic_ids(self):
        raw = config_dict(self.temp_dir)
        raw["topics"].append(dict(raw["topics"][0]))

        with self.assertRaisesRegex(ConfigError, "Duplicate topic ids"):
            parse_config(raw)

    def test_parse_config_accepts_eval_examples_and_ignores_legacy_threshold(self):
        raw = config_dict(self.temp_dir)
        raw["topics"][0].update(
            {
                "threshold": 0.99,
                "positive_examples": ["A clear positive."],
                "negative_examples": ["A clear negative."],
            }
        )

        config = parse_config(raw)

        self.assertEqual(("A clear positive.",), config.topics[0].positive_examples)
        self.assertEqual(("A clear negative.",), config.topics[0].negative_examples)
        self.assertNotIn("threshold", config.topics[0].model_dump())

    def test_parse_config_rejects_contradictory_topic_examples(self):
        raw = config_dict(self.temp_dir)
        raw["topics"][0]["positive_examples"] = ["Same example."]
        raw["topics"][0]["negative_examples"] = ["Same example."]

        with self.assertRaisesRegex(ConfigError, "both positive and negative"):
            parse_config(raw)

    def test_parse_config_is_strict_and_rejects_unknown_settings(self):
        raw = config_dict(self.temp_dir)
        raw["llm"] = {"max_tokens": "4000", "provider": "openrouter"}

        with self.assertRaisesRegex(ConfigError, "extra_forbidden"):
            parse_config(raw)

    def test_parse_config_allows_omitting_temperature_from_model_request(self):
        raw = config_dict(self.temp_dir)
        raw["llm"] = {"temperature": None}

        config = parse_config(raw)

        self.assertIsNone(config.llm.temperature)

    def test_parse_config_expands_paths_and_validates_cross_field_limits(self):
        raw = config_dict(self.temp_dir)
        raw["whatsapp"]["db_path"] = "~/ChatStorage.sqlite"
        raw["llm"] = {"max_tokens": 8000, "retry_max_tokens": 4000}

        with self.assertRaisesRegex(ConfigError, "retry_max_tokens"):
            parse_config(raw)

        raw["llm"]["retry_max_tokens"] = 8000
        config = parse_config(raw)
        self.assertEqual(Path.home() / "ChatStorage.sqlite", config.whatsapp.db_path)
