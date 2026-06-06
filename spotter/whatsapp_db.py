"""Read-only access to the local WhatsApp macOS SQLite database.

Owns the SQL queries and row → :class:`Message` conversion.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from spotter.config import WhatsAppConfig
from spotter.errors import ConfigError
from spotter.identity import clean_sender_jid, clean_sender_name, is_phone_display_name, sender_name_from_jid
from spotter.models import Message

APPLE_EPOCH_OFFSET_SECONDS = 978_307_200


@dataclass(frozen=True)
class FetchResult:
    messages: list[Message]
    fetched_high_water_pk: int | None


def open_whatsapp_db(config: WhatsAppConfig) -> sqlite3.Connection:
    """Open the local WhatsApp SQLite database in read-only mode."""
    db_path = config.db_path
    if not db_path.exists():
        raise FileNotFoundError(f"WhatsApp DB not found: {db_path}")

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def fetch_candidate_messages(
    conn: sqlite3.Connection,
    config: WhatsAppConfig,
    state: dict[str, Any],
    limit_override: int | None,
) -> FetchResult:
    """Fetch new text-bearing group messages and return them with the raw high-water mark."""
    limit = limit_override or config.max_messages_per_run
    if limit <= 0:
        raise ConfigError("Message limit must be positive.")

    last_pk = state.get("last_processed_message_pk")
    params: list[Any] = []
    cursor_sql = ""

    if isinstance(last_pk, int):
        cursor_sql = "AND m.Z_PK > ?"
        params.append(last_pk)
    else:
        min_unix_time = int((datetime.now(UTC) - timedelta(days=config.initial_backfill_days)).timestamp())
        cursor_sql = f"AND (m.ZMESSAGEDATE + {APPLE_EPOCH_OFFSET_SECONDS}) >= ?"
        params.append(min_unix_time)

    own_message_sql = "" if config.include_own_messages else "AND m.ZISFROMME = 0"
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

    messages = [message_from_row(row) for row in rows]
    fetched_high_water_pk = max((message.message_pk for message in messages), default=None)
    return FetchResult(
        messages=messages,
        fetched_high_water_pk=fetched_high_water_pk,
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


def count_groups(conn: sqlite3.Connection) -> int:
    """Count WhatsApp group chats visible in the local database."""
    row = conn.execute(
        """
        SELECT COUNT(*) AS group_count
        FROM ZWACHATSESSION s
        WHERE
            (s.ZGROUPINFO IS NOT NULL OR s.ZSESSIONTYPE IN (1, 4) OR s.ZCONTACTJID LIKE '%@g.us')
            AND s.ZCONTACTJID NOT LIKE '%@status'
        """
    ).fetchone()
    return int(row["group_count"]) if row and row["group_count"] is not None else 0


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
