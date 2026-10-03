"""FastAPI application entry point."""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app import db, demo
from app.security.auth import AuthMiddleware
from app.web import api, pages, runs

STATIC_DIR = Path(__file__).resolve().parent / "web" / "static"


@asynccontextmanager
async def lifespan(_: FastAPI):
    demo.boot()  # idempotent: schema + seed
    yield


app = FastAPI(title="Vendor Onboarding", lifespan=lifespan, docs_url=None, redoc_url=None)
app.add_middleware(AuthMiddleware)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.include_router(pages.router)
app.include_router(runs.router)
app.include_router(api.router)


@app.get("/healthz")
def healthz():
    db.query_one("SELECT 1")
    return {"status": "ok"}
