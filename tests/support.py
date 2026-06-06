from __future__ import annotations

import importlib.util
import logging
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from typing import Any

from spotter.config import AppConfig, parse_config


def load_spotter_cli() -> ModuleType:
    """Load the spotter.py CLI module once for direct unit testing."""
    existing_module = sys.modules.get("spotter_cli")
    if existing_module is not None:
        return existing_module

    spec = importlib.util.spec_from_file_location("spotter_cli", Path(__file__).parents[1] / "spotter.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load spotter.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def config_dict(temp_dir: Path) -> dict[str, Any]:
    """Return a complete synthetic Spotter configuration for tests."""
    return {
        "whatsapp": {"db_path": str(temp_dir / "ChatStorage.sqlite")},
        "files": {
            "state": str(temp_dir / "state.json"),
            "alerts": str(temp_dir / "alerts.jsonl"),
            "errors": str(temp_dir / "errors.jsonl"),
            "usage": str(temp_dir / "usage.jsonl"),
        },
        "logging": {"dir": str(temp_dir), "file": "spotter.log", "level": "INFO"},
        "notifications": {"macos": True, "pushover": False},
        "topics": [
            {
                "id": "example_topic",
                "name": "Example topic",
                "description": "A synthetic topic used only by tests.",
                "threshold": 0.75,
            }
        ],
    }


def make_config(temp_dir: Path, raw: dict[str, Any] | None = None) -> AppConfig:
    """Parse a complete synthetic configuration, optionally supplied by a test."""
    return parse_config(raw or config_dict(temp_dir))


class TestCase(unittest.TestCase):
    """Base test case that prevents application logs from being emitted."""

    def setUp(self) -> None:
        self.temp_dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        previous_disable_level = logging.root.manager.disable
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, previous_disable_level)
