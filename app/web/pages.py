"""Server-rendered pages."""
from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from app import audit, clock, db, demo, settings_store
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

@router.get("/dashboard")
def dashboard(request: Request):
    return page(request, "placeholder.html", "dashboard", title="Dashboard",
                empty_title="No submissions yet", empty_body="Run a submission from the Submit page and it will appear here.")


@router.get("/review")
def review_queue(request: Request):
    return page(request, "placeholder.html", "review", title="Review queue",
                empty_title="Nothing to review", empty_body="Submissions that need a human decision will be listed here, highest priority first.")


@router.get("/awaiting")
def awaiting(request: Request):
    return page(request, "placeholder.html", "awaiting", title="Awaiting vendor",
                empty_title="No outstanding vendor requests", empty_body="When we ask a vendor to fix something, the request and its due date appear here.")


@router.get("/rules")
def rules(request: Request):
    rows = db.query("SELECT * FROM tax_rules ORDER BY country, tax_type")
    log = db.query("SELECT * FROM tax_rule_search_log ORDER BY id DESC LIMIT 50")
    return page(request, "rules.html", "rules", rules=rows, search_log=log)


@router.get("/watchlist")
def watchlist(request: Request):
    entries = db.query("SELECT * FROM watchlist ORDER BY CASE severity WHEN 'blocked' THEN 0 WHEN 'high' THEN 1 ELSE 2 END, id")
    idents = db.query("SELECT * FROM watchlist_identifiers ORDER BY id")
    by_entry: dict[int, list] = {}
    for i in idents:
        by_entry.setdefault(i["watchlist_id"], []).append(i)
    return page(request, "watchlist.html", "watchlist", entries=entries, idents=by_entry)


# --- settings ---------------------------------------------------------------

@router.get("/settings")
def settings_page(request: Request, saved: str = ""):
    return page(request, "settings.html", "settings", settings=settings_store.all_settings(),
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
    if search_provider not in ("gemini", "fixture"):
        search_provider = "fixture"
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
def settings_clock(days: int = Form(...)):
    if days == 0:
        clock.reset()
    else:
        clock.advance(max(-365, min(365, days)))
    audit.log(_reviewer_actor(), "clock_changed", "settings", None, {"offset_days": clock.offset_days()})
    return RedirectResponse("/settings?saved=clock", status_code=303)


@router.post("/settings/reset")
def settings_reset():
    demo.reset(_reviewer_actor())
    return RedirectResponse("/settings?saved=reset", status_code=303)
