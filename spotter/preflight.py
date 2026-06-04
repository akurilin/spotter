"""Preflight checks for local runtime prerequisites."""

from __future__ import annotations

import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from spotter.errors import ConfigError


@dataclass(frozen=True)
class WhatsAppDatabaseAccess:
    """Result of checking read-only access to the configured WhatsApp database."""

    python_path: Path
    db_path: Path | None
    ok: bool
    status: str
    detail: str


def check_whatsapp_database_access(config: dict[str, Any]) -> WhatsAppDatabaseAccess:
    """Check whether the current Python process can read the WhatsApp database."""
    python_path = Path(sys.executable)
    if not python_path.is_absolute():
        python_path = (Path.cwd() / python_path).resolve()
    db_path = configured_whatsapp_database_path(config)

    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            conn.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
    except sqlite3.OperationalError as exc:
        return WhatsAppDatabaseAccess(
            python_path=python_path,
            db_path=db_path,
            ok=False,
            status="blocked",
            detail=format_database_access_failure(db_path, python_path, exc),
        )

    return WhatsAppDatabaseAccess(
        python_path=python_path,
        db_path=db_path,
        ok=True,
        status="ok",
        detail=f"Current Python can open {db_path} in read-only mode.",
    )


def configured_whatsapp_database_path(config: dict[str, Any]) -> Path:
    """Return the configured WhatsApp database path."""
    whatsapp_config = config.get("whatsapp", {})
    if not isinstance(whatsapp_config, dict):
        raise ConfigError("whatsapp config must be an object.")

    value = whatsapp_config.get("db_path")
    if not isinstance(value, str) or not value.strip():
        raise ConfigError("whatsapp.db_path must be a non-empty string.")
    return Path(value).expanduser()


def format_database_access_failure(db_path: Path, python_path: Path, exc: sqlite3.OperationalError) -> str:
    """Format a database access failure with macOS Full Disk Access guidance."""
    reason = str(exc)
    if "unable to open database file" in reason.lower():
        return (
            f"Could not open {db_path}. If WhatsApp is installed and this file exists, grant Full Disk Access "
            f"to {python_path} in System Settings."
        )
    return f"Could not read {db_path}: {reason}"
