"""Build, deduplicate, and format validated topic alerts."""

from __future__ import annotations

import json
from pathlib import Path

from spotter.config import Topic
from spotter.models import Alert, Match, Message


def build_alerts(
    topics: tuple[Topic, ...],
    messages: list[Message],
    matches: tuple[Match, ...],
    existing_alert_keys: set[tuple[int, str]],
    *,
    created_at: str,
) -> list[Alert]:
    """Turn validated topic matches into one alert per message, preferring configured topic order."""
    message_by_pk = {message.message_pk: message for message in messages}
    topic_by_id = {topic.id: topic for topic in topics}
    existing_message_pks = {message_pk for message_pk, _topic_id in existing_alert_keys}
    eligible_matches = {}
    alerts = []

    for match in matches:
        message_pk = match.message_pk
        topic_id = match.topic_id
        topic = topic_by_id[topic_id]
        confidence = match.confidence

        if confidence < topic.threshold:
            continue
        eligible_matches.setdefault((message_pk, topic_id), match)

    for message_pk, message in message_by_pk.items():
        if message_pk in existing_message_pks:
            continue

        for topic in topics:
            match = eligible_matches.get((message_pk, topic.id))
            if match is None:
                continue

            alerts.append(
                Alert(
                    created_at=created_at,
                    message_pk=message.message_pk,
                    topic_id=topic.id,
                    topic_name=topic.name,
                    confidence=round(match.confidence, 4),
                    reason=match.reason,
                    notification=match.notification,
                    group_name=message.group_name,
                    group_jid=message.group_jid,
                    sender_name=message.sender_name,
                    sender_jid=message.sender_jid,
                    local_time=message.local_time,
                    text=message.text,
                )
            )
            break

    return alerts


def read_existing_alert_keys(path: Path) -> set[tuple[int, str]]:
    """Read existing alert message/topic keys so repeated runs do not duplicate alerts."""
    keys = set()
    if not path.exists():
        return keys

    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            message_pk = row.get("message_pk")
            topic_id = row.get("topic_id")
            if isinstance(message_pk, int) and isinstance(topic_id, str):
                keys.add((message_pk, topic_id))
    return keys


def format_alert_line(alert: Alert) -> str:
    """Format one alert for readable CLI dry-run output."""
    return f"[{alert.topic_name}] {alert.group_name} / {alert.sender_name} at {alert.local_time}: {alert.text}"
