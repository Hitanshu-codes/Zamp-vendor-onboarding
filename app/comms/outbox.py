"""Outbox: render template emails, store them, and optionally deliver vendor emails through Resend.

Vendor emails contain only template text, enumerated asks and sanitised names (INV-3).
For suspicious cases no vendor-supplied string is used at all. Delivery problems are
recorded on the email and never affect a decision.
"""
from __future__ import annotations

import secrets
from datetime import timedelta
from pathlib import Path

import httpx
from jinja2 import Environment, FileSystemLoader, StrictUndefined

from app import audit, clock, db, events
from app.comms.asks import ask_for
from app.config import config
from app.decision import reason_codes
from app.security.injection import sanitize_name

TEMPLATES = Environment(loader=FileSystemLoader(str(Path(__file__).parent / "templates")), autoescape=False,
                        undefined=StrictUndefined, keep_trailing_newline=True)
RESEND_URL = "https://api.resend.com/emails"
PRIORITY_1 = {"BANK_COLLISION", "WATCHLIST_HIGH", "WATCHLIST_BLOCKED", "INJECTION_DETECTED", "SANCTIONS_FUZZY", "SANCTIONS_EXACT",
              "BANK_CHANGE_REQUEST", "UNREQUESTED_CHANGE"}


def render(template: str, **ctx) -> tuple[str, str]:
    """Returns (subject, body). The first line of every template is 'Subject: ...'."""
    text = TEMPLATES.get_template(template).render(**ctx)
    first, _, rest = text.partition("\n")
    return first.removeprefix("Subject:").strip(), rest.lstrip("\n")


def resubmit_url(token: str) -> str:
    return f"{config.public_base_url.rstrip('/')}/resubmit/{token}"


def is_suspicious(codes: list[str]) -> bool:
    return any(reason_codes.CATALOGUE[c].suspicious for c in codes if c in reason_codes.CATALOGUE)


def store(submission_id: int, run_id: int | None, kind: str, audience: str, to: str, subject: str, body: str, codes: list[str]) -> int:
    email_id = _insert_email(submission_id, run_id, kind, audience, to, subject, body, codes)
    audit.log("system", "email_prepared", "outbox_email", email_id, {"submission_id": submission_id, "kind": kind, "audience": audience})
    return email_id


def _insert_email(submission_id: int, run_id: int | None, kind: str, audience: str, to: str, subject: str, body: str, codes: list[str]) -> int:
    return db.execute(
        "INSERT INTO outbox_emails(submission_id, run_id, kind, audience, \"to\", subject, body, reason_codes_json, status, created_at)"
        " VALUES (?,?,?,?,?,?,?,?, 'draft', ?)",
        (submission_id, run_id, kind, audience, to, subject, body, db.dumps(codes), clock.now_iso()),
    )


async def deliver(email_id: int) -> dict:
    """Send a stored vendor email. Internal emails are never sent."""
    row = db.query_one("SELECT * FROM outbox_emails WHERE id = ?", (email_id,))
    if row is None or row["audience"] != "vendor":
        return {"status": "draft"}
    if config.email_provider != "resend":
        db.execute("UPDATE outbox_emails SET status = 'queued_demo_sent', provider = 'outbox' WHERE id = ?", (email_id,))
        return {"status": "queued_demo_sent", "provider": "outbox"}
    if not config.resend_api_key:
        db.execute("UPDATE outbox_emails SET status = 'failed', provider = 'resend', error = ? WHERE id = ?", ("RESEND_API_KEY not set", email_id))
        return {"status": "failed", "error": "RESEND_API_KEY not set"}
    to = config.email_redirect_to or row["to"]
    body = row["body"]
    if config.email_redirect_to:
        body = f"[Demo redirect: originally addressed to {row['to']}]\n\n{body}"
    payload = {"from": config.email_from, "to": [to], "subject": row["subject"], "text": body,
               "tags": [{"name": "kind", "value": row["kind"]}, {"name": "submission", "value": str(row["submission_id"])}]}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(RESEND_URL, json=payload, headers={
                "Authorization": f"Bearer {config.resend_api_key}", "Idempotency-Key": f"vendor-onboarding-outbox-{email_id}"})
        if resp.status_code >= 300:
            error = f"HTTP {resp.status_code}: {resp.text[:200]}"
            db.execute("UPDATE outbox_emails SET status = 'failed', provider = 'resend', delivered_to = ?, error = ? WHERE id = ?", (to, error, email_id))
            return {"status": "failed", "error": error, "delivered_to": to}
        msg_id = resp.json().get("id")
        db.execute("UPDATE outbox_emails SET status = 'sent', provider = 'resend', provider_message_id = ?, delivered_to = ?, error = NULL WHERE id = ?",
                   (msg_id, to, email_id))
        audit.log("system", "email_sent", "outbox_email", email_id, {"provider": "resend", "message_id": msg_id, "redirected": bool(config.email_redirect_to)})
        return {"status": "sent", "provider": "resend", "message_id": msg_id, "delivered_to": to}
    except httpx.HTTPError as exc:
        error = f"{type(exc).__name__}"
        db.execute("UPDATE outbox_emails SET status = 'failed', provider = 'resend', error = ? WHERE id = ?", (error, email_id))
        return {"status": "failed", "error": error}


def fixable_items(results: list, country: str) -> list[dict]:
    items = []
    for r in results:
        if r.result == "fail" and r.severity == "fixable":
            items.append({"code": r.reason_code, "check": r.check, "text": ask_for(r.reason_code, r.check, r.evidence, country)})
    return items


async def send_vendor_email(ctx_like, kind: str, template: str, codes: list[str], **extra) -> tuple[int, dict]:
    """ctx_like needs submission_id, run_id (or None) and form."""
    form = ctx_like.form
    suspicious = is_suspicious(codes) or kind in ("holding", "closure")
    names = {} if suspicious else {"contact_name": sanitize_name(form.get("contact_name")), "company": sanitize_name(form.get("legal_name"))}
    subject, body = render(template, **names, **extra)
    email_id = store(ctx_like.submission_id, ctx_like.run_id, kind, "vendor", form.get("contact_email", ""), subject, body, codes)
    result = await deliver(email_id)
    return email_id, result


async def route(ctx) -> None:
    d = ctx.decision
    if d is None:
        ctx.info("No decision was produced; the case is routed to manual review.")
        return
    codes = d.reason_codes
    results = list(ctx.results.values())
    now = clock.now()

    if d.vendor_message == "request":
        items = fixable_items(results, ctx.form.get("country", ""))
        token = db.query_one("SELECT resubmit_token FROM submissions WHERE id = ?", (ctx.submission_id,))["resubmit_token"] or secrets.token_urlsafe(18)
        due = (now + timedelta(days=config.vendor_response_due_days)).date().isoformat()
        db.execute("UPDATE submissions SET resubmit_token = ?, requested_items_json = ?, requested_at = ?, due_at = ?, reminders_sent = 0 WHERE id = ?",
                   (token, db.dumps(items), clock.now_iso(), due, ctx.submission_id))
        email_id, sent = await send_vendor_email(ctx, "request", "vendor_request.txt", codes,
                                                 items=[i["text"] for i in items], due_date=due, resubmit_url=resubmit_url(token))
        ctx.info(f"Sent one consolidated request listing {len(items)} item(s) to fix, due {due}.", email_id=email_id, delivery=sent,
                 items=[i["text"] for i in items], resubmit_path=f"/resubmit/{token}")
    elif d.vendor_message == "holding":
        email_id, sent = await send_vendor_email(ctx, "holding", "vendor_holding.txt", codes)
        case_id = open_review_case(ctx, d, tasks=case_tasks(ctx))
        ctx.info("Opened a review case. The vendor received a neutral holding notice that gives no reasons.",
                 email_id=email_id, delivery=sent, case_id=case_id)
    elif d.vendor_message == "closure":
        email_id, sent = await send_vendor_email(ctx, "closure", "vendor_closure.txt", codes)
        ctx.info("Rejected. The vendor received a neutral closure notice; the reason stays internal.", email_id=email_id, delivery=sent)
    else:
        email_id, sent = await send_vendor_email(ctx, "approval", "vendor_approval.txt", codes)
        ctx.info("Approval notice sent.", email_id=email_id, delivery=sent)


def case_tasks(ctx) -> list[str]:
    if ctx.trigger != "bank_change":
        return []
    vendor = db.query_one("SELECT v.* FROM vendors v JOIN submissions s ON s.vendor_id = v.id WHERE s.id = ?", (ctx.submission_id,))
    phone = vendor["contact_phone"] if vendor and vendor["contact_phone"] else "the number in the original onboarding records"
    return [f"Call back the vendor on the phone number already on file ({phone}). Do not use any contact details from the change request.",
            "Confirm the new account holder and number verbally before approving. Payments stay on hold until then."]


def open_review_case(ctx, d, tasks: list[str] | None = None) -> int:
    codes = d.reason_codes
    priority = 1 if set(codes) & PRIORITY_1 else (2 if is_suspicious(codes) else 3)
    existing = db.query_one("SELECT id FROM review_cases WHERE submission_id = ? AND state = 'open'", (ctx.submission_id,))
    reasons = [t.model_dump() for t in d.triggered]
    if existing:
        db.execute("UPDATE review_cases SET run_id = ?, priority = ?, reasons_json = ?, tasks_json = ? WHERE id = ?",
                   (ctx.run_id, priority, db.dumps(reasons), db.dumps(tasks or []), existing["id"]))
        case_id = existing["id"]
    else:
        case_id = db.execute("INSERT INTO review_cases(submission_id, run_id, opened_at, priority, reasons_json, tasks_json, state)"
                             " VALUES (?,?,?,?,?,?, 'open')",
                             (ctx.submission_id, ctx.run_id, clock.now_iso(), priority, db.dumps(reasons), db.dumps(tasks or [])))
    problem = [r for r in ctx.results.values() if r.result in ("fail", "warn")]
    subject, body = render("internal_brief.txt", case_id=case_id, submission_id=ctx.submission_id, version_no=ctx.version_no,
                           run_id=ctx.run_id, vendor_name=ctx.form.get("legal_name", ""), country=ctx.form.get("country", ""),
                           entity_type=ctx.form.get("entity_type", ""), status=d.status, route=d.route, risk_points=d.risk_points,
                           priority=priority, triggered=d.triggered, rationale=d.rationale_text, problem_checks=problem,
                           fixable_items=[i["text"] for i in fixable_items(list(ctx.results.values()), ctx.form.get("country", ""))],
                           case_url=f"{config.public_base_url}/review/{case_id}", run_url=f"{config.public_base_url}/runs/{ctx.run_id}")
    store(ctx.submission_id, ctx.run_id, "reviewer_brief", "internal", "review-queue", subject, body, codes)
    audit.log("system", "review_case_opened", "review_case", case_id, {"submission_id": ctx.submission_id, "priority": priority, "codes": codes})
    events.emit(ctx.run_id, "route", "info", {"message": f"Review case #{case_id} opened with priority {priority}.", "case_id": case_id})
    return case_id
