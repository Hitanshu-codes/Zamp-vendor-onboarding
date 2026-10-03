"""Review queue, case page and actions; awaiting-vendor list; vendor portal; bank change; document previews."""
from __future__ import annotations

import json

import pymupdf
from fastapi import APIRouter, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse, Response

from app import audit, db, review, scheduler
from app.config import config
from app.pipeline import forms, runner
from app.pipeline.checks import SLOT_LABEL, SLOTS
from app.web.render import page
from app.web.runs import SAMPLES, sample_index

router = APIRouter()

EVIDENCE_ROWS = [
    ("Legal name", "legal_name", "legal_name"), ("Tax ID", "tax_id", "tax_id"), ("Country", "country", "country"),
    ("Address", "registered_address", "address"), ("Account holder", "bank_account_holder", "account_holder"),
    ("Account number", "bank_account_number", "account_number"), ("IBAN", "iban", "iban"), ("Bank code", "bank_code", "bank_code"),
    ("Account type", None, "account_type"), ("Incorporated", None, "incorporation_date"), ("Tax registration", None, "registration_date"),
    ("Directors / owners", None, "directors_or_owners"),
]
PRIORITY_LABEL = {1: "High", 2: "Medium", 3: "Normal"}


def _docs_for(submission_id: int, version_no: int) -> dict[str, dict]:
    rows = db.query("SELECT d.* FROM documents d JOIN submission_versions v ON v.id = d.version_id WHERE v.submission_id = ? AND v.version_no = ?",
                    (submission_id, version_no))
    return {r["slot"]: dict(r) for r in rows}


def evidence_grid(form: dict, docs: dict[str, dict]) -> list[dict]:
    """Form value vs each document's extracted value, with mismatches marked."""
    extractions = {slot: (db.loads(d["extraction_json"], {}) or {}) for slot, d in docs.items()}
    rows = []
    for label, form_key, ex_key in EVIDENCE_ROWS:
        form_val = form.get(form_key, "") if form_key else ""
        cells = {}
        for slot in SLOTS:
            v = extractions.get(slot, {}).get(ex_key)
            cells[slot] = ", ".join(v) if isinstance(v, list) else (v or "")
        values = [x for x in [form_val, *cells.values()] if x]
        norm = {"".join(ch for ch in str(x).lower() if ch.isalnum()) for x in values}
        if any(cells.values()) or form_val:
            rows.append({"label": label, "form": form_val, "cells": cells, "mismatch": len(norm) > 1})
    return rows


@router.get("/review")
def review_queue(request: Request):
    cases = db.query("SELECT c.*, s.vendor_name, s.country, s.current_version FROM review_cases c JOIN submissions s ON s.id = c.submission_id"
                     " WHERE c.state = 'open' ORDER BY c.priority, c.opened_at")
    resolved = db.query("SELECT c.*, s.vendor_name, s.status FROM review_cases c JOIN submissions s ON s.id = c.submission_id"
                        " WHERE c.state = 'resolved' ORDER BY c.id DESC LIMIT 15")
    return page(request, "review_queue.html", "review", cases=cases, resolved=resolved, loads=db.loads, priority_label=PRIORITY_LABEL)


@router.get("/review/{case_id}")
def case_page(request: Request, case_id: int, error: str = "", done: str = ""):
    try:
        view = review.load_case(case_id)
    except review.ReviewError:
        raise HTTPException(404, "case not found")
    docs = _docs_for(view.submission["id"], view.submission["current_version"])
    checks = db.query("SELECT * FROM check_results WHERE run_id = ? AND result IN ('fail','warn') ORDER BY id", (view.case["run_id"],))
    findings = []
    for c in checks:
        ev = db.loads(c["evidence_json"], {})
        for f in ev.get("findings", []):
            findings.append(f)
        if ev.get("excerpt"):
            findings.append({"source": "LLM security filter", "rule": "contains_instructions", "excerpt": ev["excerpt"], "method": "llm"})
    grouped: dict[tuple, dict] = {}
    for f in findings:  # one entry per (source, excerpt); list every rule that fired
        g = grouped.setdefault((f.get("source"), f.get("excerpt")), {**f, "rule": f.get("rule", "")})
        if f.get("rule") and f.get("rule") not in g["rule"].split(", "):
            g["rule"] = f"{g['rule']}, {f['rule']}"
    findings = list(grouped.values())
    sanctions_hits = []
    watch_hits = []
    for c in db.query("SELECT * FROM check_results WHERE run_id = ? AND check_name IN ('sanctions','watchlist')", (view.case["run_id"],)):
        hits = db.loads(c["evidence_json"], {}).get("hits", [])
        (sanctions_hits if c["check_name"] == "sanctions" else watch_hits).extend(hits)
    cleared = {(db.loads(a["payload_json"], {}).get("list"), db.loads(a["payload_json"], {}).get("entry_id"))
               for a in review.history(case_id) if a["action"] == "clear_hit"}
    brief = db.query_one("SELECT * FROM outbox_emails WHERE run_id = ? AND kind = 'reviewer_brief' ORDER BY id DESC LIMIT 1", (view.case["run_id"],))
    rule_check = db.query_one("SELECT * FROM check_results WHERE run_id = ? AND check_name = 'tax_rule'", (view.case["run_id"],))
    return page(request, "case.html", "review", view=view, docs=docs, slots=[(s, SLOT_LABEL[s]) for s in SLOTS],
                grid=evidence_grid(view.form, docs), checks=checks, loads=db.loads, findings=findings,
                sanctions_hits=sanctions_hits, watch_hits=watch_hits, cleared=cleared, brief=brief,
                needs_confirm=review.needs_flag_confirmation(view), options=review.request_options(view),
                identifiers=review.proposed_identifiers(view), history=review.history(case_id), error=error, done=done,
                tasks=db.loads(view.case["tasks_json"], []), priority_label=PRIORITY_LABEL,
                rule_evidence=db.loads(rule_check["evidence_json"], {}) if rule_check else {})


async def _act(case_id: int, fn, done: str):
    try:
        result = fn()
        if hasattr(result, "__await__"):
            await result
    except review.ReviewError as exc:
        from urllib.parse import quote
        return RedirectResponse(f"/review/{case_id}?error={quote(str(exc))}", status_code=303)
    return RedirectResponse(f"/review/{case_id}?done={done}", status_code=303)


@router.post("/review/{case_id}/approve")
async def act_approve(case_id: int, request: Request):
    f = await request.form()
    return await _act(case_id, lambda: review.approve(case_id, str(f.get("reason", "")), f.get("confirm_flagged") == "yes"), "approved")


@router.post("/review/{case_id}/reject")
async def act_reject(case_id: int, request: Request):
    f = await request.form()
    return await _act(case_id, lambda: review.reject(case_id, str(f.get("reason", ""))), "rejected")


@router.post("/review/{case_id}/request")
async def act_request(case_id: int, request: Request):
    f = await request.form()
    return await _act(case_id, lambda: review.request_info(case_id, str(f.get("reason", "")), [str(k) for k in f.getlist("items")]), "requested")


@router.post("/review/{case_id}/clear")
async def act_clear(case_id: int, request: Request):
    f = await request.form()
    return await _act(case_id, lambda: review.clear_hit(case_id, str(f.get("list", "")), int(f.get("entry_id", 0) or 0), str(f.get("reason", ""))), "cleared")


@router.post("/review/{case_id}/watchlist")
async def act_watchlist(case_id: int, request: Request):
    f = await request.form()
    idents = []
    for raw in f.getlist("ident"):
        kind, _, value = str(raw).partition("|")
        idents.append((kind, value))
    return await _act(case_id, lambda: review.add_to_watchlist(case_id, str(f.get("reason", "")), str(f.get("severity", "")), idents,
                                                               str(f.get("expires_at", "")) or None), "watchlisted")


# --- awaiting vendor ----------------------------------------------------------

@router.get("/awaiting")
async def awaiting(request: Request):
    await scheduler.tick()
    rows = db.query("SELECT * FROM submissions WHERE status = 'pending' AND route = 'vendor_followup' ORDER BY due_at")
    closed = db.query("SELECT * FROM submissions WHERE closed_reason = 'auto_closed_no_response' ORDER BY updated_at DESC LIMIT 10")
    reminders = {r["submission_id"]: r["n"] for r in db.query("SELECT submission_id, COUNT(*) n FROM outbox_emails WHERE kind = 'reminder' GROUP BY submission_id")}
    return page(request, "awaiting.html", "awaiting", rows=rows, closed=closed, loads=db.loads, reminders=reminders)


# --- vendor portal ----------------------------------------------------------------

def _portal_submission(token: str):
    sub = db.query_one("SELECT * FROM submissions WHERE resubmit_token = ?", (token,))
    if sub is None:
        raise HTTPException(404, "This link is not valid.")
    return sub


@router.get("/resubmit/{token}")
def portal(request: Request, token: str, sent: str = ""):
    sub = _portal_submission(token)
    version = db.query_one("SELECT * FROM submission_versions WHERE submission_id = ? AND version_no = ?", (sub["id"], sub["current_version"]))
    open_for_changes = sub["status"] == "pending" and sub["route"] == "vendor_followup"
    last_run = db.query_one("SELECT id FROM runs WHERE submission_id = ? ORDER BY id DESC LIMIT 1", (sub["id"],))
    v2_samples = []
    for smp in sample_index():
        meta = json.loads((SAMPLES / smp["id"] / "expected.json").read_text(encoding="utf-8"))
        if meta.get("resubmission", {}).get("trigger", "resubmission") == "resubmission" and meta.get("resubmission"):
            v2_samples.append(smp["id"])
    return page(request, "portal.html", "awaiting", sub=sub, v2_samples=v2_samples, form=db.loads(version["form_json"], {}), fields=forms.FIELDS,
                items=db.loads(sub["requested_items_json"], []), open_for_changes=open_for_changes, slots=[(s, SLOT_LABEL[s]) for s in SLOTS],
                countries=forms.COUNTRIES, entity_types=forms.ENTITY_TYPES, categories=forms.CATEGORIES, sent=sent, token=token,
                last_run_id=last_run["id"] if last_run else None, max_mb=config.max_upload_mb)


async def _files_from(form_data) -> dict[str, tuple[str, bytes]]:
    limit = config.max_upload_mb * 1024 * 1024
    files = {}
    for slot in SLOTS:
        upload = form_data.get(slot)
        if isinstance(upload, UploadFile) or hasattr(upload, "read"):
            data = await upload.read(limit + 1)
            if data:
                files[slot] = (upload.filename or f"{slot}.pdf", data)
    return files


@router.post("/resubmit/{token}")
async def portal_submit(request: Request, token: str):
    sub = _portal_submission(token)
    if not (sub["status"] == "pending" and sub["route"] == "vendor_followup"):
        return RedirectResponse(f"/resubmit/{token}", status_code=303)
    form_data = await request.form()
    changes = {name: str(form_data.get(name) or "") for name in forms.FIELD_NAMES if name in form_data}
    files = await _files_from(form_data)
    version_id, diff = runner.create_version(sub["id"], changes, files)
    run_id = runner.start_run(sub["id"], version_id, "resubmission")
    audit.log("vendor", "resubmitted", "submission", sub["id"], {"run_id": run_id, "changed_fields": sorted(diff["form"]),
                                                                  "replaced_documents": sorted(diff["docs"])})
    return RedirectResponse(f"/resubmit/{token}?sent={run_id}", status_code=303)


# --- bank change after approval ------------------------------------------------------

@router.get("/submissions/{submission_id}/bank-change")
def bank_change_form(request: Request, submission_id: int, error: str = ""):
    sub = db.query_one("SELECT * FROM submissions WHERE id = ?", (submission_id,))
    if sub is None or not sub["vendor_id"]:
        raise HTTPException(404, "Only approved vendors can change bank details.")
    vendor = db.query_one("SELECT * FROM vendors WHERE id = ?", (sub["vendor_id"],))
    return page(request, "bank_change.html", "dashboard", sub=sub, vendor=vendor, error=error, max_mb=config.max_upload_mb)


@router.post("/submissions/{submission_id}/bank-change")
async def bank_change_submit(request: Request, submission_id: int):
    sub = db.query_one("SELECT * FROM submissions WHERE id = ?", (submission_id,))
    if sub is None or not sub["vendor_id"]:
        raise HTTPException(404, "Only approved vendors can change bank details.")
    form_data = await request.form()
    changes = {k: str(form_data.get(k) or "") for k in ("bank_account_holder", "bank_account_number", "iban", "bank_code")}
    files = await _files_from(form_data)
    if not files.get("bank_letter") or not (changes["bank_account_number"] or changes["iban"]):
        return RedirectResponse(f"/submissions/{submission_id}/bank-change?error=New+account+details+and+a+bank+letter+are+required.", status_code=303)
    db.execute("UPDATE vendors SET payment_hold = 1 WHERE id = ?", (sub["vendor_id"],))
    audit.log(f"reviewer:{review.reviewer()}", "bank_change_requested", "vendor", sub["vendor_id"], {"payment_hold": True})
    version_id, _ = runner.create_version(submission_id, changes, {"bank_letter": files["bank_letter"]})
    run_id = runner.start_run(submission_id, version_id, "bank_change")
    return RedirectResponse(f"/runs/{run_id}", status_code=303)


# --- document preview ----------------------------------------------------------------

@router.get("/documents/{doc_id}/page/{page_no}.png")
def document_page(doc_id: int, page_no: int):
    d = db.query_one("SELECT * FROM documents WHERE id = ?", (doc_id,))
    if d is None:
        raise HTTPException(404, "not found")
    data = open(d["storage_path"], "rb").read()
    if data.startswith(b"%PDF-"):
        try:
            pdf = pymupdf.open(stream=data, filetype="pdf")
            if pdf.needs_pass or not (1 <= page_no <= pdf.page_count):
                raise HTTPException(404, "page not available")
            png = pdf[page_no - 1].get_pixmap(dpi=60).tobytes("png")
            pdf.close()
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(404, "page not available")
        return Response(png, media_type="image/png", headers={"Cache-Control": "private, max-age=3600"})
    if data.startswith((b"\x89PNG", b"\xff\xd8\xff")) and page_no == 1:
        return Response(data, media_type="image/png" if data.startswith(b"\x89PNG") else "image/jpeg")
    raise HTTPException(404, "not found")
