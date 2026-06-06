"""Resolve filesystem paths derived from the scanner configuration."""

from __future__ import annotations

from pathlib import Path

from spotter.config import LoggingConfig


def app_log_dir(config: LoggingConfig) -> Path:
    """Return the directory for application and LaunchAgent logs."""
    return config.dir


def app_log_path(config: LoggingConfig) -> Path:
    """Return the main application log file path."""
    return config.path
