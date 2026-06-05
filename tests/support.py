from __future__ import annotations

import importlib.util
import logging
import sys
import unittest
from pathlib import Path
from types import ModuleType


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


class TestCase(unittest.TestCase):
    """Base test case that prevents application logs from being emitted."""

    def setUp(self) -> None:
        previous_disable_level = logging.root.manager.disable
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, previous_disable_level)
