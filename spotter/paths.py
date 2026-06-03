"""Resolve filesystem paths derived from the scanner configuration."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from spotter.errors import ConfigError


def logging_config(config: dict[str, Any]) -> dict[str, Any]:
    """Return logging settings with macOS-friendly defaults."""
    config_value = config.get("logging", {})
    if not isinstance(config_value, dict):
        raise ConfigError("logging config must be an object.")
    return config_value


def app_log_dir(config: dict[str, Any]) -> Path:
    """Return the directory for application and LaunchAgent logs."""
    value = logging_config(config).get("dir", "~/Library/Logs/spotter")
    if not isinstance(value, str) or not value.strip():
        raise ConfigError("logging.dir must be a non-empty string.")
    return Path(value).expanduser()


def app_log_path(config: dict[str, Any]) -> Path:
    """Return the main application log file path."""
    value = logging_config(config).get("file", "spotter.log")
    if not isinstance(value, str) or not value.strip():
        raise ConfigError("logging.file must be a non-empty string.")
    return app_log_dir(config) / value
