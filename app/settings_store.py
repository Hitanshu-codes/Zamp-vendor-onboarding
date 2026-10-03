"""Admin settings kept in the DB (UI toggles). Env provides the defaults."""
from __future__ import annotations

from app import db
from app.config import config

DEFAULTS: dict[str, str] = {
    "llm_enabled": "true",
    "llm_mode": config.llm_mode,
    "search_provider": config.search_provider,
    "clock_offset_days": "0",
    "reviewer_name": "Demo Reviewer",
    "evaluator_view": "false",
}


def get(key: str) -> str:
    row = db.query_one("SELECT value FROM settings WHERE key = ?", (key,))
    if row is None:
        return DEFAULTS[key]
    return row["value"]


def set_(key: str, value: str) -> None:
    if key not in DEFAULTS:
        raise KeyError(key)
    db.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def get_bool(key: str) -> bool:
    return get(key).lower() == "true"


def all_settings() -> dict[str, str]:
    return {k: get(k) for k in DEFAULTS}
