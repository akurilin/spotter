"""Send macOS and Pushover notifications for matched WhatsApp alerts."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from spotter.config import NotificationConfig
from spotter.errors import NotificationError
from spotter.identity import clean_sender_name, sender_name_from_jid
from spotter.models import Alert

LOGGER = logging.getLogger("spotter")


@dataclass(frozen=True)
class NotificationFailure:
    backend: str
    message_pk: int
    topic_id: str
    topic_name: str
    group_name: str
    error: str


def notify_alerts(config: NotificationConfig, alerts: list[Alert]) -> list[NotificationFailure]:
    """Send configured local notifications for each alert row."""
    failures: list[NotificationFailure] = []
    LOGGER.info("Sending notifications: alerts=%s macos=%s pushover=%s", len(alerts), config.macos, config.pushover)

    for alert in alerts:
        subtitle = format_notification_subtitle(alert)
        body = format_notification_body(alert, config.max_body_chars)

        if config.macos:
            try:
                send_macos_notification(
                    config.title,
                    body,
                    subtitle=subtitle,
                    sound_name=config.sound_name,
                )
            except (OSError, subprocess.CalledProcessError) as exc:
                LOGGER.warning("macOS notification failed for message %s: %s", alert.message_pk, exc)
                failures.append(build_notification_failure("macos", alert, exc))

        if config.pushover:
            try:
                send_pushover_notification(config.title, body, subtitle=subtitle, config=config)
            except NotificationError as exc:
                LOGGER.warning("Pushover notification failed for message %s: %s", alert.message_pk, exc)
                failures.append(build_notification_failure("pushover", alert, exc))

    successful_delivery_count = (len(alerts) * int(config.macos)) + (len(alerts) * int(config.pushover)) - len(failures)
    LOGGER.info("Notification delivery complete: successes=%s failures=%s", successful_delivery_count, len(failures))
    return failures


def build_notification_failure(backend: str, alert: Alert, exc: Exception) -> NotificationFailure:
    """Build a structured notification failure record without storing message text."""
    return NotificationFailure(
        backend=backend,
        message_pk=alert.message_pk,
        topic_id=alert.topic_id,
        topic_name=alert.topic_name,
        group_name=alert.group_name,
        error=str(exc),
    )


def send_test_notifications(config: NotificationConfig) -> None:
    """Send one sample notification through every enabled notification backend."""
    sample_alert = Alert(
        created_at=datetime.now(UTC).astimezone().isoformat(timespec="seconds"),
        message_pk=0,
        topic_id="test",
        topic_name="Engineering hiring",
        confidence=1,
        reason="Test notification",
        notification="Test notification",
        group_name="Example group",
        group_jid="",
        sender_name="Ali",
        sender_jid=None,
        local_time="",
        text=(
            "I am trying to design a better interview loop for founding AI engineers. "
            "Has anyone found a practical way to test judgment without a take-home?"
        ),
    )
    subtitle = format_notification_subtitle(sample_alert)
    body = format_notification_body(sample_alert, config.max_body_chars)

    if config.macos:
        send_macos_notification(config.title, body, subtitle=subtitle, sound_name=config.sound_name)

    if config.pushover:
        send_pushover_notification(config.title, body, subtitle=subtitle, config=config)


def send_macos_notification(title: str, body: str, subtitle: Any = None, sound_name: Any = None) -> None:
    """Send a macOS notification using osascript."""
    script = """
on run argv
    set notificationTitle to item 1 of argv
    set notificationBody to item 2 of argv
    set notificationSubtitle to item 3 of argv
    set notificationSound to item 4 of argv

    if notificationSound is "" then
        if notificationSubtitle is "" then
            display notification notificationBody with title notificationTitle
        else
            display notification notificationBody with title notificationTitle subtitle notificationSubtitle
        end if
    else
        if notificationSubtitle is "" then
            display notification notificationBody with title notificationTitle sound name notificationSound
        else
            display notification notificationBody with title notificationTitle subtitle notificationSubtitle sound name notificationSound
        end if
    end if
end run
"""
    subprocess.run(
        [
            "osascript",
            "-e",
            script,
            title,
            body,
            str(subtitle or ""),
            str(sound_name or ""),
        ],
        check=True,
    )


def send_pushover_notification(
    title: str,
    body: str,
    subtitle: Any = None,
    config: NotificationConfig | None = None,
) -> None:
    """Send an iOS/mobile notification through Pushover."""
    app_token = (
        os.environ.get("PUSHOVER_APP_TOKEN") or os.environ.get("PUSHOVER_API_TOKEN") or os.environ.get("PUSHOVER_TOKEN")
    )
    user_key = os.environ.get("PUSHOVER_USER_KEY") or os.environ.get("PUSHOVER_USER")
    if not app_token or not user_key:
        raise NotificationError("Pushover is enabled but PUSHOVER_APP_TOKEN and PUSHOVER_USER_KEY are not set.")

    message = body if not subtitle else f"{subtitle}\n{body}"
    payload: dict[str, Any] = {
        "token": app_token,
        "user": user_key,
        "title": title,
        "message": message,
    }

    optional_fields = {
        "device": config.pushover_device if config else None,
        "priority": config.pushover_priority if config else None,
        "sound": config.pushover_sound_name if config else None,
        "url": config.pushover_url if config else None,
        "url_title": config.pushover_url_title if config else None,
    }
    for pushover_key, value in optional_fields.items():
        if value is not None and str(value) != "":
            payload[pushover_key] = value

    encoded_payload = urllib.parse.urlencode(payload).encode("utf-8")
    request = urllib.request.Request(
        "https://api.pushover.net/1/messages.json",
        data=encoded_payload,
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            response_body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        response_body = exc.read().decode("utf-8", errors="replace")
        raise NotificationError(f"Pushover HTTP {exc.code}: {response_body}") from exc
    except urllib.error.URLError as exc:
        raise NotificationError(f"Pushover request failed: {exc.reason}") from exc
    except OSError as exc:
        raise NotificationError(f"Pushover request failed: {exc}") from exc

    try:
        parsed = json.loads(response_body)
    except json.JSONDecodeError as exc:
        raise NotificationError("Pushover returned a non-JSON response.") from exc

    if parsed.get("status") != 1:
        errors = parsed.get("errors", "unknown error")
        raise NotificationError(f"Pushover rejected notification: {errors}")


def format_notification_subtitle(alert: Alert) -> str:
    """Format the topic and group context shown below the notification title."""
    parts = [alert.topic_name, alert.group_name]
    notified_at = format_human_datetime(alert.created_at)
    if notified_at:
        parts.append(f"Notified {notified_at}")
    return " | ".join(parts)


def format_notification_body(alert: Alert, max_chars: int) -> str:
    """Format and truncate the sender and message text shown in a macOS notification."""
    text = alert.text.replace("\n", " ")
    sender_name = clean_sender_name(alert.sender_name) or sender_name_from_jid(alert.sender_jid)
    body = f"{sender_name or 'Unknown sender'}: {text}"
    if len(body) <= max_chars:
        return body
    return body[: max_chars - 1].rstrip() + "..."


def format_human_datetime(value: Any) -> str:
    """Format an ISO or SQLite local timestamp for notification display."""
    text = str(value or "").strip()
    if not text:
        return ""

    parsed_datetime = parse_datetime(text)
    if parsed_datetime is None:
        return text
    if parsed_datetime.tzinfo is not None:
        parsed_datetime = parsed_datetime.astimezone()

    hour = parsed_datetime.hour % 12 or 12
    am_pm = "AM" if parsed_datetime.hour < 12 else "PM"
    return (
        f"{parsed_datetime.strftime('%b')} {parsed_datetime.day}, {parsed_datetime.year}, "
        f"{hour}:{parsed_datetime.minute:02d}:{parsed_datetime.second:02d} {am_pm}"
    )


def parse_datetime(value: str) -> datetime | None:
    """Parse the timestamp formats written by this scanner and SQLite."""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        pass

    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
