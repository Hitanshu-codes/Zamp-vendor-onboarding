"""Jinja environment. Autoescape is always on: untrusted values are never rendered raw."""
from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from app import clock, settings_store
from app.config import config
from app.security.auth import auth_enabled

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.autoescape = True

NAV = [
    ("submit", "/submit", "Submit"),
    ("dashboard", "/dashboard", "Dashboard"),
    ("review", "/review", "Review queue"),
    ("awaiting", "/awaiting", "Awaiting vendor"),
    ("rules", "/rules", "Tax rules"),
    ("watchlist", "/watchlist", "Watchlist"),
    ("settings", "/settings", "Settings"),
]


def page(request: Request, template: str, active: str, **ctx) -> object:
    base = {
        "nav": NAV,
        "active": active,
        "reviewer": settings_store.get("reviewer_name"),
        "llm_enabled": settings_store.get_bool("llm_enabled"),
        "llm_mode": settings_store.get("llm_mode"),
        "clock_offset": clock.offset_days(),
        "sim_today": clock.today().isoformat(),
        "auth_enabled": auth_enabled(),
        "email_provider": config.email_provider,
    }
    base.update(ctx)
    return templates.TemplateResponse(request, template, base)
