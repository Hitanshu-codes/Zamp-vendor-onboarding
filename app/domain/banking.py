"""Bank identifier helpers. Full validators are added in P2."""
from __future__ import annotations

import regex


def normalize_account(value: str) -> str:
    """Uppercase, strip spaces and dashes. Used for collision and watchlist matching."""
    return regex.sub(r"[\s\-]", "", (value or "").upper())
