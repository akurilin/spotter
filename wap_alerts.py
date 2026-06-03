#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

try:
    from anthropic import Anthropic, APIConnectionError, APIStatusError, APITimeoutError
except ImportError:  # pragma: no cover - handled at runtime for clear setup errors.
    Anthropic = None  # type: ignore[assignment]
    APIConnectionError = APIStatusError = APITimeoutError = Exception  # type: ignore[misc,assignment]


APPLE_EPOCH_OFFSET_SECONDS = 978_307_200
DEFAULT_CONFIG_PATH = Path("config.json")
DEFAULT_MODEL = "claude-sonnet-4-6"


class ConfigError(RuntimeError):
    pass


class ClassificationError(RuntimeError):
    pass


class NotificationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Topic:
    id: str
    name: str
    description: str
    threshold: float


@dataclass(frozen=True)
class Message:
    message_pk: int
    group_name: str
    group_jid: str
    sender_name: str
    sender_jid: str | None
    local_time: str
    text: str


def main() -> int:
    """Parse CLI arguments and dispatch to the requested command."""
    parser = argparse.ArgumentParser(description="Scan WhatsApp groups for topic matches.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Path to config JSON.")

    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Scan new group messages and classify them.")
    run_parser.add_argument("--dry-run", action="store_true", help="Do not write state, alerts, or notifications.")
    run_parser.add_argument("--limit", type=int, help="Override max_messages_per_run for this run.")

    subparsers.add_parser("test-notification", help="Send a test macOS notification.")

    args = parser.parse_args()

    try:
        load_env_file(Path(".env"))
        config = load_config(Path(args.config))

        if args.command == "run":
            return run_scan(config, dry_run=args.dry_run, limit_override=args.limit)
        if args.command == "test-notification":
            send_test_notifications(config)
            return 0

        parser.error(f"Unknown command: {args.command}")
        return 2
    except (
        ConfigError,
        ClassificationError,
        NotificationError,
        sqlite3.Error,
        OSError,
        subprocess.CalledProcessError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def load_env_file(path: Path) -> None:
    """Load KEY=VALUE pairs from a local .env file into the process environment."""
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or key in os.environ:
            continue

        if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
            value = value[1:-1]
        os.environ[key] = value


def load_config(path: Path) -> dict[str, Any]:
    """Read and validate the scanner configuration JSON file."""
    if not path.exists():
        raise ConfigError(f"Missing config file: {path}")

    with path.open("r", encoding="utf-8") as handle:
        config = json.load(handle)

    topics = get_topics(config)
    if not topics:
        raise ConfigError("Config must contain at least one topic.")

    topic_ids = [topic.id for topic in topics]
    duplicates = sorted({topic_id for topic_id in topic_ids if topic_ids.count(topic_id) > 1})
    if duplicates:
        raise ConfigError(f"Duplicate topic ids: {', '.join(duplicates)}")

    return config


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


def run_scan(config: dict[str, Any], dry_run: bool, limit_override: int | None) -> int:
    """Run one scanner pass from WhatsApp reads through classification and optional writes."""
    state_path = config_path(config, "state")
    alerts_path = config_path(config, "alerts")
    errors_path = config_path(config, "errors")
    state = read_json_file(state_path, default={})

    with open_whatsapp_db(config) as conn:
        messages, fetched_high_water_pk = fetch_candidate_messages(conn, config, state, limit_override)
        max_group_pk = fetch_max_group_message_pk(conn)

    if not messages:
        print("No configured group messages to classify.")
        next_cursor = fetched_high_water_pk
        if next_cursor is None and state.get("last_processed_message_pk") is None:
            next_cursor = max_group_pk
        if not dry_run and next_cursor is not None:
            state["last_processed_message_pk"] = next_cursor
            state["last_run_at"] = now_iso()
            atomic_write_json(state_path, state)
            print(f"Advanced cursor to message {next_cursor}.")
        return 0

    print(f"Fetched {len(messages)} group messages for classification.", flush=True)
    existing_alert_keys = read_existing_alert_keys(alerts_path)

    try:
        matches = classify_messages(config, messages)
    except ClassificationError as exc:
        if not dry_run:
            write_error(errors_path, "classification_failed", str(exc), {"message_count": len(messages)})
        raise

    alerts = build_alerts(config, messages, matches, existing_alert_keys)
    max_processed_pk = fetched_high_water_pk or max(message.message_pk for message in messages)

    if dry_run:
        print(f"Dry-run: {len(alerts)} alert(s) would be written.")
        for alert in alerts:
            print(format_alert_line(alert))
        print(f"Dry-run: cursor would advance to message {max_processed_pk}.")
        return 0

    append_jsonl(alerts_path, alerts)
    notify_alerts(config, alerts)

    state["last_processed_message_pk"] = max_processed_pk
    state["last_run_at"] = now_iso()
    atomic_write_json(state_path, state)

    print(f"Wrote {len(alerts)} alert(s).")
    print(f"Advanced cursor to message {max_processed_pk}.")
    return 0


def config_path(config: dict[str, Any], key: str) -> Path:
    """Resolve a configured local file path such as state, alerts, or errors."""
    value = config.get("files", {}).get(key)
    if not isinstance(value, str) or not value:
        raise ConfigError(f"Missing files.{key} in config.")
    return Path(value).expanduser()


def read_json_file(path: Path, default: Any) -> Any:
    """Read a JSON file, returning the supplied default when the file is absent."""
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def atomic_write_json(path: Path, data: Any) -> None:
    """Write JSON atomically by replacing the destination after a temp-file write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
        handle.write("\n")
        temp_name = handle.name
    os.replace(temp_name, path)


def append_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    """Append dictionaries to a JSON Lines file, one encoded row per line."""
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
            handle.write("\n")


def write_error(path: Path, error_type: str, message: str, details: dict[str, Any]) -> None:
    """Record a structured scanner error in the configured JSONL error log."""
    append_jsonl(
        path,
        [
            {
                "created_at": now_iso(),
                "type": error_type,
                "message": message,
                "details": details,
            }
        ],
    )


def open_whatsapp_db(config: dict[str, Any]) -> sqlite3.Connection:
    """Open the local WhatsApp SQLite database in read-only mode."""
    db_path = Path(config["whatsapp"]["db_path"]).expanduser()
    if not db_path.exists():
        raise ConfigError(f"WhatsApp DB not found: {db_path}")

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def fetch_candidate_messages(
    conn: sqlite3.Connection,
    config: dict[str, Any],
    state: dict[str, Any],
    limit_override: int | None,
) -> tuple[list[Message], int | None]:
    """Fetch new text-bearing group messages and return them with the raw high-water mark."""
    whatsapp_config = config.get("whatsapp", {})
    include_own_messages = bool(whatsapp_config.get("include_own_messages", False))
    limit = int(limit_override or whatsapp_config.get("max_messages_per_run", 2000))
    if limit <= 0:
        raise ConfigError("Message limit must be positive.")

    last_pk = state.get("last_processed_message_pk")
    params: list[Any] = []
    cursor_sql = ""

    if isinstance(last_pk, int):
        cursor_sql = "AND m.Z_PK > ?"
        params.append(last_pk)
    else:
        backfill_days = int(whatsapp_config.get("initial_backfill_days", 14))
        if backfill_days <= 0:
            raise ConfigError("initial_backfill_days must be positive.")
        min_unix_time = int((datetime.now(UTC) - timedelta(days=backfill_days)).timestamp())
        cursor_sql = f"AND (m.ZMESSAGEDATE + {APPLE_EPOCH_OFFSET_SECONDS}) >= ?"
        params.append(min_unix_time)

    own_message_sql = "" if include_own_messages else "AND m.ZISFROMME = 0"
    params.append(limit)

    rows = conn.execute(
        f"""
        SELECT
            m.Z_PK AS message_pk,
            COALESCE(NULLIF(s.ZPARTNERNAME, ''), s.ZCONTACTJID) AS group_name,
            s.ZCONTACTJID AS group_jid,
            CASE
                WHEN m.ZISFROMME = 1 THEN 'Me'
                ELSE COALESCE(NULLIF(m.ZPUSHNAME, ''), NULLIF(p.ZPUSHNAME, ''), m.ZFROMJID, 'Unknown')
            END AS sender_name,
            m.ZFROMJID AS sender_jid,
            datetime(m.ZMESSAGEDATE + {APPLE_EPOCH_OFFSET_SECONDS}, 'unixepoch', 'localtime') AS local_time,
            m.ZTEXT AS text
        FROM ZWAMESSAGE m
        JOIN ZWACHATSESSION s ON m.ZCHATSESSION = s.Z_PK
        LEFT JOIN ZWAPROFILEPUSHNAME p ON p.ZJID = m.ZFROMJID
        WHERE
            (s.ZGROUPINFO IS NOT NULL OR s.ZSESSIONTYPE IN (1, 4) OR s.ZCONTACTJID LIKE '%@g.us')
            AND s.ZCONTACTJID NOT LIKE '%@status'
            AND m.ZMESSAGETYPE IN (0, 7)
            AND m.ZTEXT IS NOT NULL
            AND TRIM(m.ZTEXT) != ''
            {own_message_sql}
            {cursor_sql}
        ORDER BY m.Z_PK ASC
        LIMIT ?
        """,
        params,
    ).fetchall()

    raw_messages = [message_from_row(row) for row in rows]
    fetched_high_water_pk = max((message.message_pk for message in raw_messages), default=None)
    return filter_groups(raw_messages, whatsapp_config.get("groups", {})), fetched_high_water_pk


def message_from_row(row: sqlite3.Row) -> Message:
    """Convert one SQLite result row into a Message value object."""
    return Message(
        message_pk=int(row["message_pk"]),
        group_name=str(row["group_name"] or "Unknown group"),
        group_jid=str(row["group_jid"] or ""),
        sender_name=str(row["sender_name"] or "Unknown"),
        sender_jid=str(row["sender_jid"]) if row["sender_jid"] else None,
        local_time=str(row["local_time"] or ""),
        text=str(row["text"] or ""),
    )


def filter_groups(messages: list[Message], group_config: dict[str, Any]) -> list[Message]:
    """Apply configured group include and exclude filters to fetched messages."""
    include = normalize_filter_values(group_config.get("include", []))
    exclude = normalize_filter_values(group_config.get("exclude", []))

    filtered = []
    for message in messages:
        haystack = {message.group_name.casefold(), message.group_jid.casefold()}
        if include and not any(value in haystack for value in include):
            continue
        if exclude and any(value in haystack for value in exclude):
            continue
        filtered.append(message)
    return filtered


def normalize_filter_values(values: Any) -> set[str]:
    """Normalize configured group filter values for case-insensitive exact matching."""
    if not isinstance(values, list):
        return set()
    return {str(value).casefold() for value in values if str(value).strip()}


def fetch_max_group_message_pk(conn: sqlite3.Connection) -> int | None:
    """Return the highest WhatsApp message primary key seen in any group chat."""
    row = conn.execute(
        """
        SELECT MAX(m.Z_PK) AS max_pk
        FROM ZWAMESSAGE m
        JOIN ZWACHATSESSION s ON m.ZCHATSESSION = s.Z_PK
        WHERE
            (s.ZGROUPINFO IS NOT NULL OR s.ZSESSIONTYPE IN (1, 4) OR s.ZCONTACTJID LIKE '%@g.us')
            AND s.ZCONTACTJID NOT LIKE '%@status'
        """
    ).fetchone()
    return int(row["max_pk"]) if row and row["max_pk"] is not None else None


def classify_messages(config: dict[str, Any], messages: list[Message]) -> list[dict[str, Any]]:
    """Classify messages with Claude in configured batches and combine the returned matches."""
    if Anthropic is None:
        raise ClassificationError("Missing dependency: install requirements in .venv first.")

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise ClassificationError("ANTHROPIC_API_KEY is not set. Add it to .env.")

    llm_config = config.get("llm", {})
    batch_size = int(config.get("whatsapp", {}).get("batch_size", 200))
    if batch_size <= 0:
        raise ConfigError("batch_size must be positive.")

    client = Anthropic(
        api_key=api_key,
        timeout=float(llm_config.get("timeout_seconds", 120)),
        max_retries=int(llm_config.get("max_retries", 3)),
    )

    all_matches: list[dict[str, Any]] = []
    topics = get_topics(config)
    for batch in chunks(messages, batch_size):
        all_matches.extend(classify_batch(client, config, topics, batch))
    return all_matches


def classify_batch(
    client: Any, config: dict[str, Any], topics: list[Topic], batch: list[Message]
) -> list[dict[str, Any]]:
    """Send one message batch to Claude and validate the sparse match response."""
    llm_config = config.get("llm", {})
    payload = {
        "topics": [topic.__dict__ for topic in topics],
        "valid_message_pks": [message.message_pk for message in batch],
        "messages": [message.__dict__ for message in batch],
    }

    kwargs = {
        "model": str(llm_config.get("model", DEFAULT_MODEL)),
        "max_tokens": int(llm_config.get("max_tokens", 1500)),
        "temperature": float(llm_config.get("temperature", 0)),
        "system": system_prompt(),
        "messages": [
            {
                "role": "user",
                "content": json.dumps(payload, ensure_ascii=False),
            }
        ],
    }

    if bool(llm_config.get("use_output_config", True)):
        kwargs["output_config"] = {"format": {"type": "json_schema", "schema": matches_schema()}}

    try:
        try:
            response = client.messages.create(**kwargs)
        except TypeError as exc:
            if "output_config" not in str(exc):
                raise
            kwargs.pop("output_config", None)
            response = client.messages.create(**kwargs)
    except (APIConnectionError, APITimeoutError) as exc:
        raise ClassificationError(f"Anthropic connection failure after retries: {exc}") from exc
    except APIStatusError as exc:
        status_code = getattr(exc, "status_code", "unknown")
        request_id = getattr(exc, "request_id", None)
        suffix = f" request_id={request_id}" if request_id else ""
        raise ClassificationError(f"Anthropic API status error {status_code}.{suffix} {exc}") from exc

    parsed = getattr(response, "parsed_output", None)
    if parsed is None:
        parsed = parse_json_response(response)

    return validate_matches(parsed, {message.message_pk for message in batch}, {topic.id for topic in topics})


def system_prompt() -> str:
    """Build the system instruction used for sparse topic classification."""
    return (
        "You classify WhatsApp group messages for topic-based user alerts. "
        "Return only messages that clearly match at least one configured topic. "
        "Do not score or return non-matching messages. "
        "Use the exact message_pk and topic id values from the input. "
        "Never invent, approximate, or alter message_pk values; omit a match if the exact id is not present. "
        "Confidence must be a number from 0 to 1. "
        "Return JSON with one top-level key named matches."
    )


def matches_schema() -> dict[str, Any]:
    """Return the JSON schema requested from Claude for structured match output."""
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "matches": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "message_pk": {"type": "integer"},
                        "topic_id": {"type": "string"},
                        "confidence": {"type": "number"},
                        "reason": {"type": "string"},
                        "notification": {"type": "string"},
                    },
                    "required": ["message_pk", "topic_id", "confidence", "reason", "notification"],
                },
            }
        },
        "required": ["matches"],
    }


def parse_json_response(response: Any) -> dict[str, Any]:
    """Parse a non-structured Claude response body as JSON."""
    text_parts = []
    for block in getattr(response, "content", []):
        text = getattr(block, "text", None)
        if text:
            text_parts.append(text)

    raw_text = "".join(text_parts).strip()
    if not raw_text:
        raise ClassificationError("Anthropic returned no text content.")

    try:
        return json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ClassificationError(f"Could not parse Anthropic JSON response: {exc}") from exc


def validate_matches(parsed: Any, message_pks: set[int], topic_ids: set[str]) -> list[dict[str, Any]]:
    """Validate Claude matches and drop malformed or hallucinated individual matches."""
    if not isinstance(parsed, dict) or not isinstance(parsed.get("matches"), list):
        raise ClassificationError("Anthropic response must be an object with a matches array.")

    valid_matches = []
    for item in parsed["matches"]:
        if not isinstance(item, dict):
            raise ClassificationError("Each match must be an object.")

        try:
            message_pk = int(item.get("message_pk"))
            confidence = float(item.get("confidence"))
        except (TypeError, ValueError):
            print(f"warning: ignoring malformed Anthropic match: {item}", file=sys.stderr)
            continue

        topic_id = str(item.get("topic_id", ""))
        reason = str(item.get("reason", "")).strip()
        notification = str(item.get("notification", "")).strip()

        if message_pk not in message_pks:
            print(f"warning: ignoring Anthropic match with unknown message_pk: {message_pk}", file=sys.stderr)
            continue
        if topic_id not in topic_ids:
            print(f"warning: ignoring Anthropic match with unknown topic_id: {topic_id}", file=sys.stderr)
            continue
        if not 0 <= confidence <= 1:
            print(f"warning: ignoring Anthropic match with invalid confidence: {confidence}", file=sys.stderr)
            continue

        valid_matches.append(
            {
                "message_pk": message_pk,
                "topic_id": topic_id,
                "confidence": confidence,
                "reason": reason,
                "notification": notification,
            }
        )
    return valid_matches


def build_alerts(
    config: dict[str, Any],
    messages: list[Message],
    matches: list[dict[str, Any]],
    existing_alert_keys: set[tuple[int, str]],
) -> list[dict[str, Any]]:
    """Turn validated topic matches into alert rows after thresholds and dedupe checks."""
    message_by_pk = {message.message_pk: message for message in messages}
    topic_by_id = {topic.id: topic for topic in get_topics(config)}
    alerts = []

    for match in matches:
        message_pk = int(match["message_pk"])
        topic_id = str(match["topic_id"])
        topic = topic_by_id[topic_id]
        confidence = float(match["confidence"])
        alert_key = (message_pk, topic_id)

        if confidence < topic.threshold or alert_key in existing_alert_keys:
            continue

        message = message_by_pk[message_pk]
        alerts.append(
            {
                "created_at": now_iso(),
                "message_pk": message.message_pk,
                "topic_id": topic.id,
                "topic_name": topic.name,
                "confidence": round(confidence, 4),
                "reason": str(match["reason"]),
                "notification": str(match["notification"]),
                "group_name": message.group_name,
                "group_jid": message.group_jid,
                "sender_name": message.sender_name,
                "sender_jid": message.sender_jid,
                "local_time": message.local_time,
                "text": message.text,
            }
        )

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


def notify_alerts(config: dict[str, Any], alerts: list[dict[str, Any]]) -> None:
    """Send configured local notifications for each alert row."""
    notification_config = config.get("notifications", {})
    title = str(notification_config.get("title", "WhatsApp topic match"))
    max_body_chars = int(notification_config.get("max_body_chars", 180))
    macos_enabled = bool(notification_config.get("macos", True))
    pushover_enabled = bool(notification_config.get("pushover", False))

    for alert in alerts:
        subtitle = format_notification_subtitle(alert)
        body = format_notification_body(alert, max_body_chars)

        if macos_enabled:
            try:
                send_macos_notification(
                    title,
                    body,
                    subtitle=subtitle,
                    sound_name=notification_config.get("sound_name"),
                )
            except (OSError, subprocess.CalledProcessError) as exc:
                print(f"warning: macOS notification failed for message {alert['message_pk']}: {exc}", file=sys.stderr)

        if pushover_enabled:
            try:
                send_pushover_notification(title, body, subtitle=subtitle, notification_config=notification_config)
            except NotificationError as exc:
                print(
                    f"warning: Pushover notification failed for message {alert['message_pk']}: {exc}", file=sys.stderr
                )


def send_test_notifications(config: dict[str, Any]) -> None:
    """Send one sample notification through every enabled notification backend."""
    notification_config = config.get("notifications", {})
    title = str(notification_config.get("title", "WhatsApp topic match"))
    subtitle = "Engineering hiring | YC CTOs (active)"
    body = (
        "Ali: I am trying to design a better interview loop for founding AI engineers. "
        "Has anyone found a practical way to test judgment without a take-home?"
    )

    if notification_config.get("macos", True):
        send_macos_notification(title, body, subtitle=subtitle, sound_name=notification_config.get("sound_name"))

    if notification_config.get("pushover", False):
        send_pushover_notification(title, body, subtitle=subtitle, notification_config=notification_config)


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
    notification_config: dict[str, Any] | None = None,
) -> None:
    """Send an iOS/mobile notification through Pushover."""
    app_token = (
        os.environ.get("PUSHOVER_APP_TOKEN") or os.environ.get("PUSHOVER_API_TOKEN") or os.environ.get("PUSHOVER_TOKEN")
    )
    user_key = os.environ.get("PUSHOVER_USER_KEY") or os.environ.get("PUSHOVER_USER")
    if not app_token or not user_key:
        raise NotificationError("Pushover is enabled but PUSHOVER_APP_TOKEN and PUSHOVER_USER_KEY are not set.")

    config = notification_config or {}
    message = body if not subtitle else f"{subtitle}\n{body}"
    payload: dict[str, Any] = {
        "token": app_token,
        "user": user_key,
        "title": title,
        "message": message,
    }

    optional_fields = {
        "device": "pushover_device",
        "priority": "pushover_priority",
        "sound": "pushover_sound_name",
        "url": "pushover_url",
        "url_title": "pushover_url_title",
    }
    for pushover_key, config_key in optional_fields.items():
        value = config.get(config_key)
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


def format_notification_subtitle(alert: dict[str, Any]) -> str:
    """Format the topic and group context shown below the notification title."""
    return f"{alert['topic_name']} | {alert['group_name']}"


def format_notification_body(alert: dict[str, Any], max_chars: int) -> str:
    """Format and truncate the sender and message text shown in a macOS notification."""
    text = str(alert["text"]).replace("\n", " ")
    body = f"{alert['sender_name']}: {text}"
    if len(body) <= max_chars:
        return body
    return body[: max_chars - 1].rstrip() + "..."


def format_alert_line(alert: dict[str, Any]) -> str:
    """Format one alert for readable CLI dry-run output."""
    return (
        f"[{alert['topic_name']}] {alert['group_name']} / {alert['sender_name']} "
        f"at {alert['local_time']}: {alert['text']}"
    )


def chunks(items: list[Message], size: int) -> list[list[Message]]:
    """Split a list of messages into fixed-size batches."""
    return [items[index : index + size] for index in range(0, len(items), size)]


def now_iso() -> str:
    """Return the current local timestamp in ISO-8601 format."""
    return datetime.now(UTC).astimezone().isoformat(timespec="seconds")


if __name__ == "__main__":
    raise SystemExit(main())
