#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from spotter.classifier import DEFAULT_MODEL, classify_messages
from spotter.errors import ClassificationError, ConfigError, LaunchAgentError, NotificationError
from spotter.launchagent import install_launch_agent, show_launch_agent_status, uninstall_launch_agent
from spotter.notifications import NotificationFailure, notify_alerts, send_test_notifications
from spotter.paths import app_log_path, logging_config
from spotter.types import get_topics
from spotter.usage import UsageAccumulator, UsageRecord, new_run_id, write_usage_record
from spotter.whatsapp_db import (
    Message,
    count_groups,
    fetch_candidate_messages,
    fetch_max_group_message_pk,
    fetch_message_local_time,
    open_whatsapp_db,
)

DEFAULT_CONFIG_PATH = Path("config.json")
LOGGER = logging.getLogger("spotter")


def main() -> int:
    """Parse CLI arguments and dispatch to the requested command."""
    parser = argparse.ArgumentParser(description="Scan WhatsApp groups for topic matches.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Path to config JSON.")

    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Scan new group messages and classify them.")
    run_parser.add_argument("--dry-run", action="store_true", help="Do not write state, alerts, or notifications.")
    run_parser.add_argument("--limit", type=int, help="Override max_messages_per_run for this run.")

    subparsers.add_parser("test-notification", help="Send a test macOS notification.")
    subparsers.add_parser("install-agent", help="Install or update the per-user macOS LaunchAgent.")
    subparsers.add_parser("uninstall-agent", help="Unload and remove the per-user macOS LaunchAgent.")
    subparsers.add_parser("agent-status", help="Show the current LaunchAgent status.")
    subparsers.add_parser("tui", help="Open the terminal UI.")

    args = parser.parse_args()

    try:
        config_path = resolve_config_path(Path(args.config))
        load_env_file(config_path.parent / ".env")
        config = load_config(config_path)
        configure_logging(config)
        LOGGER.debug("Command started: %s", args.command)

        if args.command == "run":
            return run_scan(config, dry_run=args.dry_run, limit_override=args.limit)
        if args.command == "test-notification":
            send_test_notifications(config)
            return 0
        if args.command == "install-agent":
            return install_launch_agent(config, config_path)
        if args.command == "uninstall-agent":
            return uninstall_launch_agent(config)
        if args.command == "agent-status":
            return show_launch_agent_status(config, config_path)
        if args.command == "tui":
            from spotter.tui import run_tui

            return run_tui(config, config_path)

        parser.error(f"Unknown command: {args.command}")
        return 2
    except (
        ConfigError,
        ClassificationError,
        LaunchAgentError,
        NotificationError,
        sqlite3.Error,
        OSError,
        subprocess.CalledProcessError,
    ) as exc:
        if LOGGER.handlers:
            LOGGER.exception("Command failed.")
        print(f"error: {exc}", file=sys.stderr)
        return 1


def resolve_config_path(path: Path) -> Path:
    """Resolve the config path relative to the current directory."""
    expanded_path = path.expanduser()
    if expanded_path.is_absolute():
        return expanded_path
    return (Path.cwd() / expanded_path).resolve()


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
        raise ConfigError(f"Missing config file: {path}. Copy config.example.json to {path} and edit your topics.")

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


def configure_logging(config: dict[str, Any]) -> None:
    """Configure console and file logging for interactive and LaunchAgent runs."""
    log_path = app_log_path(config)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    level_name = str(logging_config(config).get("level", "INFO")).upper()
    level = getattr(logging, level_name, logging.INFO)

    LOGGER.handlers.clear()
    LOGGER.setLevel(level)
    LOGGER.propagate = False

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    file_handler.setLevel(level)
    LOGGER.addHandler(file_handler)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(logging.Formatter("%(message)s"))
    console_handler.setLevel(level)
    LOGGER.addHandler(console_handler)


def run_scan(config: dict[str, Any], dry_run: bool, limit_override: int | None) -> int:
    """Run one scanner pass from WhatsApp reads through classification and optional writes."""
    state_path = config_path(config, "state")
    alerts_path = config_path(config, "alerts")
    errors_path = config_path(config, "errors")
    usage_path = optional_config_path(config, "usage")
    state = read_json_file(state_path, default={})
    last_pk = state.get("last_processed_message_pk")
    topics = get_topics(config)
    llm_config = config.get("llm", {})
    whatsapp_config = config.get("whatsapp", {})
    model = str(llm_config.get("model", DEFAULT_MODEL))

    run_id = new_run_id()
    started_at = now_iso()
    accumulator = UsageAccumulator()
    status = "ok"
    message_count = 0
    alert_count = 0

    LOGGER.info(
        "Scanner run starting: run_id=%s dry_run=%s limit_override=%s model=%s batch_size=%s topics=%s",
        run_id,
        dry_run,
        limit_override,
        model,
        whatsapp_config.get("batch_size", 200),
        len(topics),
    )

    try:
        with open_whatsapp_db(config) as conn:
            cursor_time = fetch_message_local_time(conn, last_pk) if isinstance(last_pk, int) else None
            group_count = count_groups(conn)
            fetch_result = fetch_candidate_messages(conn, config, state, limit_override)
            max_group_pk = fetch_max_group_message_pk(conn)

        messages = fetch_result.messages
        message_count = len(messages)
        fetched_high_water_pk = fetch_result.fetched_high_water_pk

        if isinstance(last_pk, int):
            LOGGER.info("Cursor before scan: message_pk=%s local_time=%s", last_pk, cursor_time or "unknown")
        else:
            LOGGER.info(
                "Cursor before scan: empty; using initial_backfill_days=%s",
                whatsapp_config.get("initial_backfill_days", 14),
            )
        LOGGER.info(
            "WhatsApp scan scope: groups=%s messages=%s high_water_pk=%s",
            group_count,
            message_count,
            fetched_high_water_pk,
        )

        if not messages:
            status = "no_messages"
            LOGGER.info("No new group messages to classify.")
            next_cursor = fetched_high_water_pk
            if next_cursor is None and state.get("last_processed_message_pk") is None:
                next_cursor = max_group_pk
            if not dry_run and next_cursor is not None:
                state["last_processed_message_pk"] = next_cursor
                state["last_run_at"] = now_iso()
                atomic_write_json(state_path, state)
                LOGGER.info("Advanced cursor to message %s.", next_cursor)
            return 0

        LOGGER.info("Fetched %s group messages for classification.", message_count)
        existing_alert_keys = read_existing_alert_keys(alerts_path)

        try:
            matches = classify_messages(config, messages, accumulator)
        except ClassificationError as exc:
            status = "classification_failed"
            if not dry_run:
                write_error(errors_path, "classification_failed", str(exc), {"message_count": len(messages)})
            LOGGER.exception("Classification failed; cursor will not advance.")
            raise

        alerts = build_alerts(config, messages, matches, existing_alert_keys)
        alert_count = len(alerts)
        max_processed_pk = fetched_high_water_pk or max(message.message_pk for message in messages)
        LOGGER.info("Classification complete: matches=%s alerts_after_thresholds=%s", len(matches), alert_count)

        if dry_run:
            LOGGER.info("Dry-run: %s alert(s) would be written.", alert_count)
            for alert in alerts:
                print(format_alert_line(alert))
            LOGGER.info("Dry-run: cursor would advance to message %s.", max_processed_pk)
            return 0

        append_jsonl(alerts_path, alerts)
        notification_failures = notify_alerts(config, alerts)
        if notification_failures:
            try:
                write_notification_failures(errors_path, notification_failures)
                LOGGER.warning("Recorded %s notification failure(s) in %s.", len(notification_failures), errors_path)
            except OSError as exc:
                LOGGER.warning("Could not write notification failures to %s: %s", errors_path, exc)

        state["last_processed_message_pk"] = max_processed_pk
        state["last_run_at"] = now_iso()
        atomic_write_json(state_path, state)

        LOGGER.info("Wrote %s alert(s).", alert_count)
        LOGGER.info("Advanced cursor to message %s.", max_processed_pk)
        return 0
    except Exception:
        if status == "ok":
            status = "error"
        raise
    finally:
        if usage_path is not None:
            try:
                write_usage_record(
                    usage_path,
                    UsageRecord(
                        run_id=run_id,
                        started_at=started_at,
                        completed_at=now_iso(),
                        model=model,
                        dry_run=dry_run,
                        status=status,
                        messages=message_count,
                        batches=accumulator.batches,
                        alerts=alert_count,
                        input_tokens=accumulator.input_tokens,
                        output_tokens=accumulator.output_tokens,
                        cache_creation_input_tokens=accumulator.cache_creation_input_tokens,
                        cache_read_input_tokens=accumulator.cache_read_input_tokens,
                    ),
                )
            except OSError as exc:
                LOGGER.warning("Could not write usage record to %s: %s", usage_path, exc)


def config_path(config: dict[str, Any], key: str) -> Path:
    """Resolve a configured local file path such as state, alerts, or errors."""
    value = config.get("files", {}).get(key)
    if not isinstance(value, str) or not value:
        raise ConfigError(f"Missing files.{key} in config.")
    return Path(value).expanduser()


def optional_config_path(config: dict[str, Any], key: str) -> Path | None:
    """Resolve a configured local file path, returning None when unset."""
    value = config.get("files", {}).get(key)
    if not isinstance(value, str) or not value:
        return None
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


def write_notification_failures(path: Path, failures: list[NotificationFailure]) -> None:
    """Record notification delivery failures without duplicating message bodies."""
    append_jsonl(
        path,
        [
            {
                "created_at": now_iso(),
                "type": "notification_failed",
                "message": failure.error,
                "details": failure.__dict__,
            }
            for failure in failures
        ],
    )


def build_alerts(
    config: dict[str, Any],
    messages: list[Message],
    matches: list[dict[str, Any]],
    existing_alert_keys: set[tuple[int, str]],
) -> list[dict[str, Any]]:
    """Turn validated topic matches into one alert per message, preferring configured topic order."""
    message_by_pk = {message.message_pk: message for message in messages}
    topics = get_topics(config)
    topic_by_id = {topic.id: topic for topic in topics}
    existing_message_pks = {message_pk for message_pk, _topic_id in existing_alert_keys}
    eligible_matches = {}
    alerts = []

    for match in matches:
        message_pk = int(match["message_pk"])
        topic_id = str(match["topic_id"])
        topic = topic_by_id[topic_id]
        confidence = float(match["confidence"])

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

            confidence = float(match["confidence"])
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


def format_alert_line(alert: dict[str, Any]) -> str:
    """Format one alert for readable CLI dry-run output."""
    return (
        f"[{alert['topic_name']}] {alert['group_name']} / {alert['sender_name']} "
        f"at {alert['local_time']}: {alert['text']}"
    )


def now_iso() -> str:
    """Return the current local timestamp in ISO-8601 format."""
    return datetime.now(UTC).astimezone().isoformat(timespec="seconds")


if __name__ == "__main__":
    raise SystemExit(main())
