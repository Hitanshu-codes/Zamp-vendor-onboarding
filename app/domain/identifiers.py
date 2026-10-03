"""Normalise identifiers so exact matching is format-insensitive (watchlist, CSV import)."""
from __future__ import annotations

import regex

from app.domain.banking import normalize_account
from app.domain.names import normalize_name


def normalize_identifier(kind: str, value: str) -> str:
    if kind in ("name", "director"):
        return normalize_name(value)
    if kind == "bank_account":
        return normalize_account(value)
    if kind == "tax_id":
        return regex.sub(r"\s", "", (value or "").upper())
    if kind == "email_domain":
        v = (value or "").strip().lower()
        return v.rsplit("@", 1)[-1]
    if kind == "phone":
        return regex.sub(r"[^\d]", "", value or "")
    return " ".join(regex.sub(r"[^\p{L}\p{N}\s]", " ", (value or "").lower()).split())
