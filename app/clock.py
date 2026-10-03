"""now() with a simulated day offset, so reminders and auto-close can be demoed in seconds."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from app import settings_store


def offset_days() -> int:
    return int(settings_store.get("clock_offset_days"))


def now() -> datetime:
    return datetime.now(timezone.utc) + timedelta(days=offset_days())


def today() -> date:
    return now().date()


def now_iso() -> str:
    return now().isoformat(timespec="seconds")


def advance(days: int) -> int:
    new = offset_days() + days
    settings_store.set_("clock_offset_days", str(new))
    return new


def reset() -> None:
    settings_store.set_("clock_offset_days", "0")
