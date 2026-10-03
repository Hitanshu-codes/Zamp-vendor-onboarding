"""Stage 11: Finalise. Update the submission, close the run, write the audit trail."""
from __future__ import annotations

from app import audit, clock, db, events
from app.domain.banking import normalize_account
from app.domain.names import normalize_name
from app.domain.validators import email_domain
from app.pipeline import forms
from app.pipeline.context import RunContext


def add_vendor(submission_id: int, form: dict) -> int:
    """An approved submission becomes an active vendor (so later duplicates and collisions are caught)."""
    row = db.query_one("SELECT vendor_id FROM submissions WHERE id = ?", (submission_id,))
    if row and row["vendor_id"]:
        return row["vendor_id"]
    vid = db.execute(
        "INSERT INTO vendors(legal_name, normalized_name, country, tax_id, bank_account, bank_code, status, email_domain,"
        " source_submission_id, created_at) VALUES (?,?,?,?,?,?, 'active', ?,?,?)",
        (form["legal_name"], normalize_name(form["legal_name"]), form["country"], form["tax_id"],
         normalize_account(forms.account(form)), form["bank_code"], email_domain(form["contact_email"]), submission_id, clock.now_iso()),
    )
    db.execute("UPDATE submissions SET vendor_id = ? WHERE id = ?", (vid, submission_id))
    return vid


async def run(ctx: RunContext) -> None:
    d = ctx.decision
    now = clock.now_iso()
    top = [t.code for t in d.triggered][:5] if d else ["CHECK_MISSING"]
    status, route = (d.status, d.route) if d else ("pending", "manual_review")
    db.execute(
        "UPDATE submissions SET status = ?, route = ?, risk_points = ?, top_reason_codes_json = ?, vendor_name = ?, country = ?,"
        " updated_at = ? WHERE id = ?",
        (status, route, d.risk_points if d else 0, db.dumps(top), ctx.form.get("legal_name", "")[:200], ctx.form.get("country", ""),
         now, ctx.submission_id),
    )
    if status == "approved" and ctx.trigger != "bank_change":
        add_vendor(ctx.submission_id, ctx.form)
    db.execute("UPDATE runs SET finished_at = ?, outcome_status = ?, outcome_route = ? WHERE id = ?", (now, status, route, ctx.run_id))
    audit.log("system", "run_finished", "submission", ctx.submission_id,
              {"run_id": ctx.run_id, "version": ctx.version_no, "status": status, "route": route, "codes": top})
    ctx.info("Run complete. Submission record, decision and audit trail updated.", status=status, route=route)


def emit_run_finished(ctx: RunContext) -> None:
    d = ctx.decision
    events.emit(ctx.run_id, "finalise", "run_finished", {
        "status": d.status if d else "pending", "route": d.route if d else "manual_review",
        "reason_codes": [t.code for t in d.triggered] if d else ["CHECK_MISSING"], "submission_id": ctx.submission_id,
    })
