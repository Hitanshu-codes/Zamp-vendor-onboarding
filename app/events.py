"""Event bus: every event is persisted to run_events first, then pushed to live SSE subscribers.

Finished runs are replayed straight from the DB, so a replay is identical to the live view.
"""
from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from typing import Any

from app import clock, db


@dataclass
class Event:
    run_id: int
    seq: int
    ts: str
    stage: str
    kind: str
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"run_id": self.run_id, "seq": self.seq, "ts": self.ts, "stage": self.stage, "kind": self.kind, "payload": self.payload}


_subs_lock = threading.Lock()
_subscribers: dict[int, list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]]] = {}


def emit(run_id: int, stage: str, kind: str, payload: dict[str, Any] | None = None) -> Event:
    payload = payload or {}
    ts = clock.now_iso()
    with db.connect() as conn:
        row = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM run_events WHERE run_id = ?", (run_id,)).fetchone()
        seq = row[0]
        conn.execute(
            "INSERT INTO run_events(run_id, seq, ts, stage, kind, payload_json) VALUES (?,?,?,?,?,?)",
            (run_id, seq, ts, stage, kind, db.dumps(payload)),
        )
    event = Event(run_id, seq, ts, stage, kind, payload)
    with _subs_lock:
        targets = list(_subscribers.get(run_id, []))
    for loop, queue in targets:
        loop.call_soon_threadsafe(queue.put_nowait, event)
    return event


def history(run_id: int, after_seq: int = 0) -> list[Event]:
    rows = db.query(
        "SELECT run_id, seq, ts, stage, kind, payload_json FROM run_events WHERE run_id = ? AND seq > ? ORDER BY seq",
        (run_id, after_seq),
    )
    return [Event(r["run_id"], r["seq"], r["ts"], r["stage"], r["kind"], db.loads(r["payload_json"], {})) for r in rows]


def subscribe(run_id: int) -> asyncio.Queue:
    queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()
    with _subs_lock:
        _subscribers.setdefault(run_id, []).append((loop, queue))
    return queue


def unsubscribe(run_id: int, queue: asyncio.Queue) -> None:
    with _subs_lock:
        subs = _subscribers.get(run_id, [])
        _subscribers[run_id] = [(lp, q) for lp, q in subs if q is not queue]
        if not _subscribers[run_id]:
            del _subscribers[run_id]


def run_is_finished(run_id: int) -> bool:
    row = db.query_one("SELECT finished_at FROM runs WHERE id = ?", (run_id,))
    return row is not None and row["finished_at"] is not None
