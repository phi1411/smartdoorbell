from __future__ import annotations

from datetime import datetime, timezone


def sqlite_utc_to_iso(value: str | None) -> str | None:
    """Expose SQLite UTC timestamps as unambiguous ISO-8601 values."""
    if not value:
        return value
    text = str(value).strip()
    if text.endswith("Z") or "+" in text[10:]:
        return text
    return text.replace(" ", "T", 1) + "Z"


def utc_iso_to_local_time(value: str | None, fallback: str = "--:--") -> str:
    if not value:
        return fallback
    try:
        normalized = sqlite_utc_to_iso(value)
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone().strftime("%H:%M")
    except (TypeError, ValueError):
        return fallback

