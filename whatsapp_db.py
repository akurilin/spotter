"""Read-only access to the local WhatsApp macOS SQLite database.

Owns the SQL queries, the row → :class:`Message` conversion, and the sender-name
and group-filter helpers that only make sense in the context of WhatsApp's
schema.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from errors import ConfigError

APPLE_EPOCH_OFFSET_SECONDS = 978_307_200
BASE64ISH_SENDER_RE = re.compile(r"^[A-Za-z0-9+/]{4,}={0,2}$")
JID_SUFFIXES = ("@s.whatsapp.net", "@lid", "@g.us", "@status")


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
class FetchResult:
    messages: list[Message]
    fetched_high_water_pk: int | None
    raw_message_count: int


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
) -> FetchResult:
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
        WITH latest_profile_push_name AS (
            SELECT pp.ZJID, pp.ZPUSHNAME
            FROM ZWAPROFILEPUSHNAME pp
            JOIN (
                SELECT ZJID, MAX(Z_PK) AS max_pk
                FROM ZWAPROFILEPUSHNAME
                WHERE ZPUSHNAME IS NOT NULL AND ZPUSHNAME != ''
                GROUP BY ZJID
            ) latest_pp ON latest_pp.ZJID = pp.ZJID AND latest_pp.max_pk = pp.Z_PK
        )
        SELECT
            m.Z_PK AS message_pk,
            COALESCE(NULLIF(s.ZPARTNERNAME, ''), s.ZCONTACTJID) AS group_name,
            s.ZCONTACTJID AS group_jid,
            m.ZISFROMME AS is_from_me,
            gm.ZMEMBERJID AS sender_member_jid,
            sender_session.ZCONTACTIDENTIFIER AS sender_session_identifier,
            m.ZFROMJID AS sender_from_jid,
            gm.ZCONTACTNAME AS sender_contact_name,
            gm.ZFIRSTNAME AS sender_first_name,
            sender_session.ZPARTNERNAME AS sender_session_name,
            member_push.ZPUSHNAME AS sender_profile_push_name,
            linked_push.ZPUSHNAME AS sender_linked_profile_push_name,
            datetime(m.ZMESSAGEDATE + {APPLE_EPOCH_OFFSET_SECONDS}, 'unixepoch', 'localtime') AS local_time,
            m.ZTEXT AS text
        FROM ZWAMESSAGE m
        JOIN ZWACHATSESSION s ON m.ZCHATSESSION = s.Z_PK
        LEFT JOIN ZWAGROUPMEMBER gm ON gm.Z_PK = m.ZGROUPMEMBER
        LEFT JOIN ZWACHATSESSION sender_session ON sender_session.ZCONTACTJID = gm.ZMEMBERJID
        LEFT JOIN latest_profile_push_name member_push ON member_push.ZJID = gm.ZMEMBERJID
        LEFT JOIN latest_profile_push_name linked_push ON linked_push.ZJID = sender_session.ZCONTACTIDENTIFIER
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
    return FetchResult(
        messages=filter_groups(raw_messages, whatsapp_config.get("groups", {})),
        fetched_high_water_pk=fetched_high_water_pk,
        raw_message_count=len(raw_messages),
    )


def fetch_message_local_time(conn: sqlite3.Connection, message_pk: Any) -> str | None:
    """Return the local timestamp for a WhatsApp message primary key."""
    if not isinstance(message_pk, int):
        return None
    row = conn.execute(
        f"""
        SELECT datetime(ZMESSAGEDATE + {APPLE_EPOCH_OFFSET_SECONDS}, 'unixepoch', 'localtime') AS local_time
        FROM ZWAMESSAGE
        WHERE Z_PK = ?
        """,
        [message_pk],
    ).fetchone()
    if not row or row["local_time"] is None:
        return None
    return str(row["local_time"])


def count_configured_groups(conn: sqlite3.Connection, config: dict[str, Any]) -> int:
    """Count WhatsApp groups that match the current include/exclude configuration."""
    rows = conn.execute(
        """
        SELECT
            COALESCE(NULLIF(s.ZPARTNERNAME, ''), s.ZCONTACTJID) AS group_name,
            s.ZCONTACTJID AS group_jid
        FROM ZWACHATSESSION s
        WHERE
            (s.ZGROUPINFO IS NOT NULL OR s.ZSESSIONTYPE IN (1, 4) OR s.ZCONTACTJID LIKE '%@g.us')
            AND s.ZCONTACTJID NOT LIKE '%@status'
        """
    ).fetchall()
    group_config = config.get("whatsapp", {}).get("groups", {})
    return sum(
        1 for row in rows if group_is_included(str(row["group_name"] or ""), str(row["group_jid"] or ""), group_config)
    )


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


def message_from_row(row: sqlite3.Row) -> Message:
    """Convert one SQLite result row into a Message value object."""
    return Message(
        message_pk=int(row["message_pk"]),
        group_name=str(row["group_name"] or "Unknown group"),
        group_jid=str(row["group_jid"] or ""),
        sender_name=resolve_sender_name(row),
        sender_jid=resolve_sender_jid(row),
        local_time=str(row["local_time"] or ""),
        text=str(row["text"] or ""),
    )


def resolve_sender_name(row: sqlite3.Row) -> str:
    """Return the first useful sender display name for a WhatsApp group message."""
    if int(row["is_from_me"] or 0) == 1:
        return "Me"

    phone_fallback = ""
    for key in (
        "sender_contact_name",
        "sender_first_name",
        "sender_session_name",
        "sender_profile_push_name",
        "sender_linked_profile_push_name",
    ):
        name = clean_sender_name(row[key])
        if not name:
            continue
        if is_phone_display_name(name):
            phone_fallback = phone_fallback or name
            continue
        return name

    if phone_fallback:
        return phone_fallback
    return sender_name_from_jid(resolve_sender_jid(row)) or "Unknown sender"


def resolve_sender_jid(row: sqlite3.Row) -> str | None:
    """Return the most useful sender JID, avoiding the group JID for group messages."""
    candidates = (
        clean_sender_jid(row["sender_session_identifier"]),
        clean_sender_jid(row["sender_member_jid"]),
        clean_sender_jid(row["sender_from_jid"]),
    )
    for jid in candidates:
        if jid and jid.endswith("@s.whatsapp.net"):
            return jid
    for jid in candidates:
        if jid and not (jid.endswith("@g.us") or jid.endswith("@status")):
            return jid
    return None


def clean_sender_name(value: Any) -> str:
    """Normalize a sender name and reject raw identifiers."""
    name = " ".join(str(value or "").split())
    if not name or is_unhelpful_sender_name(name):
        return ""
    return name


def clean_sender_jid(value: Any) -> str:
    """Normalize a sender JID-ish value."""
    jid = str(value or "").strip()
    return jid if jid else ""


def is_unhelpful_sender_name(name: str) -> bool:
    """Return whether a candidate sender name is a raw WhatsApp identifier."""
    lowered = name.casefold()
    if lowered in {"unknown", "unknown sender"}:
        return True
    if is_phone_display_name(name):
        return False
    if any(suffix in lowered for suffix in JID_SUFFIXES):
        return True
    return is_base64ish_sender_name(name)


def is_base64ish_sender_name(name: str) -> bool:
    """Return whether a name looks like the opaque tokens WhatsApp stores in ZPUSHNAME."""
    if " " in name or not BASE64ISH_SENDER_RE.fullmatch(name):
        return False
    if name.endswith("="):
        return True
    if any(character in name for character in "+/"):
        return True
    return len(name) >= 16 and any(character.isdigit() for character in name)


def is_phone_display_name(name: str) -> bool:
    """Return whether a display name is just a formatted phone number."""
    digits = "".join(character for character in name if character.isdigit())
    if len(digits) < 7:
        return False
    allowed_characters = set(" +().-")
    return all(character.isdigit() or character in allowed_characters for character in name)


def sender_name_from_jid(jid: str | None) -> str:
    """Use a phone JID as a last-resort readable sender label."""
    if not jid or not jid.endswith("@s.whatsapp.net"):
        return ""
    digits = jid.split("@", 1)[0]
    if not digits.isdigit():
        return ""
    return format_phone_digits(digits)


def format_phone_digits(digits: str) -> str:
    """Format phone-number digits from a WhatsApp JID."""
    if len(digits) == 11 and digits.startswith("1"):
        return f"+1 {digits[1:4]} {digits[4:7]} {digits[7:]}"
    return f"+{digits}"


def filter_groups(messages: list[Message], group_config: dict[str, Any]) -> list[Message]:
    """Apply configured group include and exclude filters to fetched messages."""
    return [message for message in messages if group_is_included(message.group_name, message.group_jid, group_config)]


def group_is_included(group_name: str, group_jid: str, group_config: dict[str, Any]) -> bool:
    """Return whether a group name/JID passes configured include and exclude filters."""
    include = normalize_filter_values(group_config.get("include", []))
    exclude = normalize_filter_values(group_config.get("exclude", []))
    haystack = {group_name.casefold(), group_jid.casefold()}
    if include and not any(value in haystack for value in include):
        return False
    return not (exclude and any(value in haystack for value in exclude))


def normalize_filter_values(values: Any) -> set[str]:
    """Normalize configured group filter values for case-insensitive exact matching."""
    if not isinstance(values, list):
        return set()
    return {str(value).casefold() for value in values if str(value).strip()}
