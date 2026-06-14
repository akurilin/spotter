"""Shared domain values passed between Spotter subsystems."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from spotter.usage import UsageAccumulator


@dataclass(frozen=True)
class Message:
    message_pk: int
    group_name: str
    group_jid: str
    sender_name: str
    sender_jid: str | None
    local_time: str
    text: str


@dataclass(frozen=True)
class Match:
    message_pk: int
    topic_id: str
    reason: str
    notification: str


@dataclass(frozen=True)
class Alert:
    created_at: str
    message_pk: int
    topic_id: str
    topic_name: str
    reason: str
    notification: str
    group_name: str
    group_jid: str
    sender_name: str
    sender_jid: str | None
    local_time: str
    text: str

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-compatible alert record written to alerts.jsonl."""
        return asdict(self)


@dataclass(frozen=True)
class ClassificationResult:
    matches: tuple[Match, ...]
    usage: UsageAccumulator
