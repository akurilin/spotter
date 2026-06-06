from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from spotter.errors import ConfigError


@dataclass(frozen=True)
class Topic:
    id: str
    name: str
    description: str
    threshold: float


def get_topics(config: dict[str, Any]) -> list[Topic]:
    """Convert configured topic dictionaries into typed Topic objects."""
    topics = []
    for item in config.get("topics", []):
        topics.append(
            Topic(
                id=require_str(item, "id"),
                name=require_str(item, "name"),
                description=require_str(item, "description"),
                threshold=float(item.get("threshold", 0.75)),
            )
        )
    return topics


def require_str(data: dict[str, Any], key: str) -> str:
    """Return a required non-empty string field or raise a configuration error."""
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"Expected non-empty string for topic.{key}")
    return value.strip()
