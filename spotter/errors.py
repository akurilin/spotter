"""Shared exception types raised across the WhatsApp alerts package."""

from __future__ import annotations


class ConfigError(RuntimeError):
    pass


class ClassificationError(RuntimeError):
    pass


class NotificationError(RuntimeError):
    pass


class LaunchAgentError(RuntimeError):
    pass


class MonitoringError(RuntimeError):
    pass
