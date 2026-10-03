"""Stage 10: Route and communicate. Vendor emails and the reviewer case are built from templates (comms.outbox)."""
from __future__ import annotations

from app.comms import outbox
from app.pipeline.context import RunContext


async def run(ctx: RunContext) -> None:
    await outbox.route(ctx)
