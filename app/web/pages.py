"""Server-rendered pages."""
from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from app import audit, clock, db, demo, scheduler, settings_store, tax_registry, watchlist_admin
from app.config import config
from app.security.auth import COOKIE, SESSION_MAX_AGE, auth_enabled, make_cookie, password_ok
from app.web.render import page, templates

router = APIRouter()


def _reviewer_actor() -> str:
    return f"reviewer:{settings_store.get('reviewer_name')}"


@router.get("/")
def home():
    return RedirectResponse("/submit", status_code=303)


# --- auth -------------------------------------------------------------------

@router.get("/login")
def login_form(request: Request, next: str = "/submit", error: str = ""):
    return templates.TemplateResponse(request, "login.html", {"next": next, "error": error})


@router.post("/login")
def login(request: Request, password: str = Form(...), next: str = Form("/submit")):
    if not auth_enabled() or password_ok(password):
        target = next if next.startswith("/") and not next.startswith("//") else "/submit"
        resp = RedirectResponse(target, status_code=303)
        resp.set_cookie(COOKIE, make_cookie(), max_age=SESSION_MAX_AGE, httponly=True, samesite="lax",
                        secure=request.url.scheme == "https")
        return resp
    return templates.TemplateResponse(request, "login.html", {"next": next, "error": "Incorrect password."}, status_code=401)


@router.get("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE)
    return resp


# --- main sections (filled in by later phases) ---------------------------------

@router.get("/rules")
def rules(request: Request, error: str = "", done: str = ""):
    rows = db.query("SELECT * FROM tax_rules ORDER BY country, tax_type, id")
    log = db.query("SELECT * FROM tax_rule_search_log ORDER BY id DESC LIMIT 50")
    hits = sum(r["times_used"] for r in rows)
    searches = db.query_one("SELECT COUNT(*) c FROM tax_rule_search_log")["c"]
    return page(request, "rules.html", "rules", rules=rows, search_log=log, reverify={r["id"]: tax_registry.reverify_due(r) for r in rows},
                error=error, done=done, cache_hits=hits, searches=searches, loads=db.loads, promote_n=config.rule_promote_after_n_approved)


@router.post("/rules/{rule_id}/{action}")
async def rule_action(rule_id: int, action: str, request: Request):
    f = await request.form()
    reason = " ".join(str(f.get("reason", "")).split())
    actor = _reviewer_actor()
    try:
        if action != "forget" and len(reason) < 5:
            raise ValueError("Please type a reason; it is stored in the audit trail.")
        if action == "verify":
            tax_registry.verify(rule_id, actor, reason)
        elif action == "disable":
            tax_registry.disable(rule_id, actor, reason)
        elif action == "enable":
            tax_registry.enable(rule_id, actor, reason)
        elif action == "edit":
            report = tax_registry.edit(rule_id, actor, reason, str(f.get("regex", "")).strip(), str(f.get("example", "")).strip(),
                                       str(f.get("description", "")).strip())
            if not report.passed:
                raise ValueError("Edited rule failed the self-test: " + "; ".join(report.reasons))
        elif action == "forget":
            tax_registry.forget(rule_id, actor)
        else:
            raise ValueError("Unknown action.")
    except ValueError as exc:
        from urllib.parse import quote
        return RedirectResponse(f"/rules?error={quote(str(exc))}#rule-{rule_id}", status_code=303)
    return RedirectResponse(f"/rules?done={action}#rule-{rule_id}", status_code=303)


@router.get("/watchlist")
def watchlist(request: Request, error: str = "", done: str = ""):
    entries = db.query("SELECT * FROM watchlist ORDER BY active DESC, CASE severity WHEN 'blocked' THEN 0 WHEN 'high' THEN 1 ELSE 2 END, id")
    by_entry: dict[int, list] = {}
    for i in db.query("SELECT * FROM watchlist_identifiers ORDER BY id"):
        by_entry.setdefault(i["watchlist_id"], []).append(i)
    matches: dict[int, list] = {}
    for m in db.query("SELECT m.*, r.submission_id, s.vendor_name FROM watchlist_matches m JOIN runs r ON r.id = m.run_id"
                      " JOIN submissions s ON s.id = r.submission_id ORDER BY m.id DESC"):
        matches.setdefault(m["watchlist_id"], []).append(m)
    today = clock.today().isoformat()
    return page(request, "watchlist.html", "watchlist", entries=entries, idents=by_entry, matches=matches, error=error, done=done,
                kinds=watchlist_admin.KINDS, today=today)


def _back(error: str | None = None, done: str | None = None):
    from urllib.parse import quote
    return RedirectResponse(f"/watchlist?{'error=' + quote(error) if error else 'done=' + quote(done or 'saved')}", status_code=303)


@router.post("/watchlist/add")
async def watchlist_add(request: Request):
    f = await request.form()
    idents = [(str(k), str(v)) for k, v in zip(f.getlist("kind"), f.getlist("value"))]
    try:
        watchlist_admin.add_entry(str(f.get("label", "")), str(f.get("severity", "")), str(f.get("reason", "")), idents,
                                  str(f.get("expires_at", "")) or None, _reviewer_actor())
    except watchlist_admin.WatchlistError as exc:
        return _back(error=str(exc))
    return _back(done="Entry added")


@router.post("/watchlist/{entry_id}/active")
async def watchlist_toggle(request: Request, entry_id: int):
    f = await request.form()
    try:
        watchlist_admin.set_active(entry_id, f.get("active") == "1", str(f.get("reason", "")), _reviewer_actor())
    except watchlist_admin.WatchlistError as exc:
        return _back(error=str(exc))
    return _back(done="Entry updated")


@router.post("/watchlist/import")
async def watchlist_import(request: Request):
    f = await request.form()
    upload = f.get("csv")
    if upload is None or not hasattr(upload, "read"):
        return _back(error="Choose a CSV file.")
    raw = (await upload.read(512_000)).decode("utf-8-sig", errors="replace")
    try:
        created, errors = watchlist_admin.import_csv(raw, _reviewer_actor())
    except watchlist_admin.WatchlistError as exc:
        return _back(error=str(exc))
    msg = f"Imported {created} entr{'y' if created == 1 else 'ies'}" + (f"; {len(errors)} problem(s): " + " | ".join(errors[:3]) if errors else "")
    return _back(done=msg) if created or not errors else _back(error=msg)


# --- settings ---------------------------------------------------------------

@router.get("/settings")
def settings_page(request: Request, saved: str = "", reminders: int = 0, closed: int = 0):
    return page(request, "settings.html", "settings", settings=settings_store.all_settings(), tick={"reminders": reminders, "closed": closed},
                thresholds=config.thresholds(), saved=saved, models=(config.model_extract, config.model_judge),
                gemini_key_set=bool(config.gemini_api_key), resend_key_set=bool(config.resend_api_key),
                email_redirect=config.email_redirect_to)


@router.post("/settings")
def settings_save(
    request: Request,
    llm_enabled: str = Form("false"),
    llm_mode: str = Form("replay"),
    search_provider: str = Form("gemini"),
    reviewer_name: str = Form("Demo Reviewer"),
    evaluator_view: str = Form("false"),
):
    if llm_mode not in ("live", "record", "replay"):
        llm_mode = "replay"
    if search_provider not in ("gemini", "directory", "fixture"):
        search_provider = "directory"
    reviewer_name = " ".join(reviewer_name.split())[:60] or "Demo Reviewer"
    actor = _reviewer_actor()  # attribute the change to whoever made it, before any rename
    before = settings_store.all_settings()
    settings_store.set_("llm_enabled", "true" if llm_enabled == "true" else "false")
    settings_store.set_("llm_mode", llm_mode)
    settings_store.set_("search_provider", search_provider)
    settings_store.set_("evaluator_view", "true" if evaluator_view == "true" else "false")
    settings_store.set_("reviewer_name", reviewer_name)
    after = settings_store.all_settings()
    changed = {k: [before[k], after[k]] for k in after if before[k] != after[k]}
    if changed:
        audit.log(actor, "settings_changed", "settings", None, changed)
    return RedirectResponse("/settings?saved=1", status_code=303)


@router.post("/settings/clock")
async def settings_clock(days: int = Form(...)):
    if days == 0:
        clock.reset()
    else:
        clock.advance(max(-365, min(365, days)))
    audit.log(_reviewer_actor(), "clock_changed", "settings", None, {"offset_days": clock.offset_days()})
    result = await scheduler.tick()  # reminders and auto-close follow the simulated clock
    return RedirectResponse(f"/settings?saved=clock&reminders={result['reminders']}&closed={result['closed']}", status_code=303)


@router.post("/settings/reset")
def settings_reset():
    demo.reset(_reviewer_actor())
    return RedirectResponse("/settings?saved=reset", status_code=303)
