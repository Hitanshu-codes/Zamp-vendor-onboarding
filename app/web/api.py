"""JSON + SSE endpoints for the live run view."""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from app import db, events

router = APIRouter(prefix="/api")

KEEPALIVE_SECONDS = 15


def _sse(event: events.Event) -> str:
    return f"id: {event.seq}\nevent: {event.kind}\ndata: {json.dumps(event.to_dict(), ensure_ascii=False)}\n\n"


@router.get("/runs/{run_id}/events")
async def run_events_stream(run_id: int, request: Request, after: int = 0):
    if db.query_one("SELECT id FROM runs WHERE id = ?", (run_id,)) is None:
        raise HTTPException(404, "run not found")
    header_last = request.headers.get("last-event-id")
    last = int(header_last) if header_last and header_last.isdigit() else after

    async def gen():
        nonlocal last
        # Subscribe before replaying history so nothing emitted in between is lost;
        # duplicates are dropped by comparing seq numbers.
        queue = events.subscribe(run_id)
        try:
            for ev in events.history(run_id, last):
                last = ev.seq
                yield _sse(ev)
                if ev.kind == "run_finished":
                    yield "event: end\ndata: {}\n\n"
                    return
            while True:
                if await request.is_disconnected():
                    return
                try:
                    ev = await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_SECONDS)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if ev.seq <= last:
                    continue
                last = ev.seq
                yield _sse(ev)
                if ev.kind == "run_finished":
                    yield "event: end\ndata: {}\n\n"
                    return
        finally:
            events.unsubscribe(run_id, queue)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/runs/{run_id}/events.json")
def run_events_poll(run_id: int, after: int = 0):
    """Polling fallback for clients where SSE is blocked."""
    if db.query_one("SELECT id FROM runs WHERE id = ?", (run_id,)) is None:
        raise HTTPException(404, "run not found")
    evs = events.history(run_id, after)
    return {"events": [e.to_dict() for e in evs], "finished": events.run_is_finished(run_id)}
