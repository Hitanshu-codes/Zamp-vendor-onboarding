"""Submit page, sample scenarios, live run view and email preview."""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse

from app import db, settings_store
from app.config import ROOT, config
from app.pipeline import forms, runner
from app.pipeline.checks import SLOT_LABEL, SLOTS
from app.security.auth import submission_limiter
from app.web.render import page

router = APIRouter()
SAMPLES = ROOT / "samples"


def sample_index() -> list[dict]:
    path = SAMPLES / "index.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def _sample_dir(sid: str) -> Path:
    if sid not in {s["id"] for s in sample_index()}:
        raise HTTPException(404, "unknown sample")
    return SAMPLES / sid


@router.get("/submit")
def submit_page(request: Request, error: str = ""):
    return page(request, "submit.html", "submit", fields=forms.FIELDS, countries=forms.COUNTRIES, entity_types=forms.ENTITY_TYPES,
                categories=forms.CATEGORIES, slots=[(s, SLOT_LABEL[s]) for s in SLOTS], samples=sample_index(),
                evaluator_view=settings_store.get_bool("evaluator_view"), error=error, max_mb=config.max_upload_mb)


@router.get("/api/samples/{sid}")
def sample_detail(sid: str):
    folder = _sample_dir(sid)
    meta = json.loads((folder / "expected.json").read_text(encoding="utf-8"))
    files = [p.name for p in sorted(folder.glob("*")) if p.suffix in (".pdf", ".png", ".jpg") and not p.stem.endswith("_v2")]
    return {"id": sid, "form": json.loads((folder / "form.json").read_text(encoding="utf-8")), "files": files,
            "title": meta["title"], "notes": meta.get("notes", ""), "expected": meta["expected"]}


@router.get("/samples/{sid}/{name}")
def sample_file(sid: str, name: str):
    folder = _sample_dir(sid)
    path = (folder / name).resolve()
    if path.parent != folder.resolve() or path.suffix not in (".pdf", ".png", ".jpg") or not path.exists():
        raise HTTPException(404, "not found")
    return FileResponse(path)


@router.post("/submit")
async def submit(request: Request):
    client_ip = request.client.host if request.client else "unknown"
    if not submission_limiter.allow(client_ip):
        return RedirectResponse("/submit?error=Too+many+submissions+from+your+address.+Please+wait+and+try+again.", status_code=303)
    form_data = await request.form()
    raw = {name: str(form_data.get(name) or "") for name in forms.FIELD_NAMES}
    limit = config.max_upload_mb * 1024 * 1024
    files: dict[str, tuple[str, bytes]] = {}
    for slot in SLOTS:
        upload = form_data.get(slot)
        if isinstance(upload, UploadFile) or hasattr(upload, "read"):
            data = await upload.read(limit + 1)
            if data:
                files[slot] = (upload.filename or f"{slot}.pdf", data)  # size cap is enforced by intake (limit + 1 bytes read)
    sub_id, ver_id = runner.create_submission(raw, files)
    run_id = runner.start_run(sub_id, ver_id, "initial")
    return RedirectResponse(f"/runs/{run_id}", status_code=303)


@router.get("/runs/{run_id}")
def run_view(request: Request, run_id: int):
    run = db.query_one("SELECT * FROM runs WHERE id = ?", (run_id,))
    if run is None:
        raise HTTPException(404, "run not found")
    sub = db.query_one("SELECT * FROM submissions WHERE id = ?", (run["submission_id"],))
    version = db.query_one("SELECT * FROM submission_versions WHERE id = ?", (run["version_id"],))
    form = db.loads(version["form_json"], {})
    other_runs = db.query("SELECT r.id, r.trigger, r.started_at, r.outcome_status, v.version_no FROM runs r JOIN submission_versions v"
                          " ON v.id = r.version_id WHERE r.submission_id = ? ORDER BY r.id", (sub["id"],))
    return page(request, "run.html", "submit", run=run, sub=sub, version=version, form=form, stages=runner.STAGE_INFO,
                other_runs=other_runs, country_name=forms.COUNTRIES.get(form.get("country", ""), form.get("country", "")))


@router.get("/emails/{email_id}")
def email_view(request: Request, email_id: int):
    email = db.query_one("SELECT * FROM outbox_emails WHERE id = ?", (email_id,))
    if email is None:
        raise HTTPException(404, "email not found")
    return page(request, "email.html", "dashboard", email=email, codes=db.loads(email["reason_codes_json"], []))
