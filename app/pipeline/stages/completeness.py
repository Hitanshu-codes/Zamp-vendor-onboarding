"""Stage 3: Completeness. All required documents are present."""
from __future__ import annotations

from app.models import failed, passed
from app.pipeline.checks import SLOT_LABEL, SLOTS
from app.pipeline.context import RunContext


async def run(ctx: RunContext) -> None:
    sole = ctx.form.get("entity_type") == "sole_proprietor"
    for slot in SLOTS:
        def outcome(s=slot):
            if s in ctx.docs:
                return passed(f"A {SLOT_LABEL[s]} was provided ({ctx.docs[s].filename}).")
            if s == "incorporation_cert" and sole:
                return passed("Not required: sole proprietors have no incorporation certificate.", waived=True)
            return failed("DOC_MISSING", f"The {SLOT_LABEL[s]} is missing.", slot=s)
        await ctx.check(f"doc_present.{slot}", outcome)
