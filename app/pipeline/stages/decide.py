"""Stage 9: Decide. The pure decision engine turns check results into a status."""
from __future__ import annotations

from app import audit, clock, db, events
from app.config import config
from app.decision.engine import decide
from app.models import DecisionFlags
from app.pipeline import checks
from app.pipeline.context import RunContext


async def run(ctx: RunContext) -> None:
    flags = DecisionFlags(
        required_checks=checks.required_checks(ctx.form),
        injection_flag=ctx.injection_flag,
        llm_failure=ctx.llm_failure,
        risk_threshold=config.risk_points_review_threshold,
    )
    decision = decide(list(ctx.results.values()), flags)
    ctx.decision = decision
    db.execute(
        "INSERT INTO decisions(run_id, submission_id, ts, status, route, reason_codes_json, risk_points, rationale_text, decided_by)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (ctx.run_id, ctx.submission_id, clock.now_iso(), decision.status, decision.route,
         db.dumps([t.model_dump() for t in decision.triggered]), decision.risk_points, decision.rationale_text, "engine"),
    )
    audit.log("system", "decision_made", "run", ctx.run_id, {"status": decision.status, "route": decision.route,
                                                            "codes": decision.reason_codes, "risk_points": decision.risk_points})
    events.emit(ctx.run_id, "decide", "info", {
        "message": "Decision made by the rules engine (no LLM involved).",
        "decision": decision.model_dump(),
        "flags": {"injection_flag": flags.injection_flag, "llm_failure": flags.llm_failure, "llm_failures": ctx.llm_failures,
                  "required_checks": len(flags.required_checks), "risk_threshold": flags.risk_threshold},
    })
