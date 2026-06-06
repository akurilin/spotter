"""Normalize WhatsApp sender identities for database reads and display."""

from __future__ import annotations

import re
from typing import Any

BASE64ISH_SENDER_RE = re.compile(r"^[A-Za-z0-9+/]{4,}={0,2}$")
JID_SUFFIXES = ("@s.whatsapp.net", "@lid", "@g.us", "@status")


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
