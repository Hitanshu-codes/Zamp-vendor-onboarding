"""Append-only audit log. There is deliberately no update or delete function."""
from __future__ import annotations

from typing import Any

from app import clock, db


def log(actor: str, action: str, entity_type: str | None = None, entity_id: Any = None, detail: dict | None = None) -> None:
    db.execute(
        "INSERT INTO audit_log(ts, actor, action, entity_type, entity_id, detail_json) VALUES (?,?,?,?,?,?)",
        (clock.now_iso(), actor, action, entity_type, None if entity_id is None else str(entity_id), db.dumps(detail or {})),
    )
