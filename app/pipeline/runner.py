"""Creates submissions/versions, starts runs, and executes the 11 stages in order."""
from __future__ import annotations

import asyncio
import hashlib

from app import audit, clock, db, events
from app.config import config
from app.models import CheckResult
from app.pipeline import checks, forms
from app.pipeline.context import DocInfo, RunContext, run_stage
from app.pipeline.stages import (classify_extract, completeness, cross_reference, decide, finalise, injection_scan, intake, risk_screen,
                                 route, tax_rule_lookup, validate)
from app.security.injection import sanitize_filename

STAGES = [
    ("intake", "Intake", "Files and form fields are checked", intake.run),
    ("injection_scan", "Injection scan", "Untrusted text is scanned for instructions and hidden content", injection_scan.run),
    ("completeness", "Completeness", "All required documents are present", completeness.run),
    ("classify_extract", "Classify & extract", "The LLM reads each document (content only, fixed schema)", classify_extract.run),
    ("tax_rule_lookup", "Tax rule lookup", "Find, or learn, the tax ID format for this country", tax_rule_lookup.run),
    ("validate", "Validate", "Deterministic format, checksum and date checks", validate.run),
    ("cross_reference", "Cross-reference", "Names, account holder and country agree across sources", cross_reference.run),
    ("risk_screen", "Risk screening", "Sanctions, watchlist, duplicates and bank collisions", risk_screen.run),
    ("decide", "Decide", "Rules engine sets the status (no LLM)", decide.run),
    ("route", "Route & communicate", "Vendor email and reviewer case", route.run),
    ("finalise", "Finalise", "Records, audit trail and dashboard updated", finalise.run),
]
STAGE_INFO = [{"key": k, "label": label, "description": desc} for k, label, desc, _ in STAGES]
_tasks: set[asyncio.Task] = set()


# --- storage ------------------------------------------------------------------

def save_upload(data: bytes) -> tuple[str, str]:
    sha = hashlib.sha256(data).hexdigest()
    config.uploads_dir.mkdir(parents=True, exist_ok=True)
    path = config.uploads_dir / f"{sha}.bin"
    if not path.exists():
        path.write_bytes(data)
    return sha, str(path)


def _insert_documents(version_id: int, files: dict[str, tuple[str, bytes]]) -> None:
    for slot, (filename, data) in files.items():
        sha, path = save_upload(data)
        db.execute("INSERT INTO documents(version_id, slot, filename, storage_path, sha256, size) VALUES (?,?,?,?,?,?)",
                   (version_id, slot, sanitize_filename(filename), path, sha, len(data)))


def create_submission(raw_form: dict, files: dict[str, tuple[str, bytes]], vendor_ref: str | None = None) -> tuple[int, int]:
    form = forms.normalize_form(raw_form)
    now = clock.now_iso()
    sid = db.execute("INSERT INTO submissions(created_at, updated_at, vendor_name, country, vendor_ref) VALUES (?,?,?,?,?)",
                     (now, now, form["legal_name"][:200], form["country"], vendor_ref))
    vid = db.execute("INSERT INTO submission_versions(submission_id, version_no, form_json, created_at) VALUES (?, 1, ?, ?)",
                     (sid, db.dumps(form), now))
    _insert_documents(vid, files)
    audit.log("system", "submission_created", "submission", sid, {"version": 1, "documents": sorted(files)})
    return sid, vid


def create_version(submission_id: int, raw_form: dict, new_files: dict[str, tuple[str, bytes]]) -> tuple[int, dict]:
    """A new version: changed form fields + replaced documents; unchanged documents carry over. Returns (version_id, diff)."""
    sub = db.query_one("SELECT current_version FROM submissions WHERE id = ?", (submission_id,))
    prev = db.query_one("SELECT * FROM submission_versions WHERE submission_id = ? AND version_no = ?", (submission_id, sub["current_version"]))
    old_form = db.loads(prev["form_json"], {})
    form = forms.normalize_form({**old_form, **raw_form})
    old_docs = {d["slot"]: d for d in db.query("SELECT * FROM documents WHERE version_id = ?", (prev["id"],))}
    diff_form = {k: {"old": old_form.get(k, ""), "new": form[k]} for k in form if form[k] != old_form.get(k, "")}
    diff_docs = {}
    for slot, (filename, data) in new_files.items():
        sha = hashlib.sha256(data).hexdigest()
        if slot not in old_docs or old_docs[slot]["sha256"] != sha:
            diff_docs[slot] = {"old": old_docs[slot]["filename"] if slot in old_docs else None, "new": sanitize_filename(filename)}
    diff = {"form": diff_form, "docs": diff_docs}
    version_no = sub["current_version"] + 1
    now = clock.now_iso()
    vid = db.execute("INSERT INTO submission_versions(submission_id, version_no, form_json, created_at, diff_from_previous_json) VALUES (?,?,?,?,?)",
                     (submission_id, version_no, db.dumps(form), now, db.dumps(diff)))
    carried = {slot: (d["filename"], open(d["storage_path"], "rb").read()) for slot, d in old_docs.items() if slot not in new_files}
    _insert_documents(vid, {**carried, **new_files})
    db.execute("UPDATE submissions SET current_version = ?, updated_at = ? WHERE id = ?", (version_no, now, submission_id))
    audit.log("system", "version_created", "submission", submission_id, {"version": version_no, "changed_fields": sorted(diff_form),
                                                                          "replaced_documents": sorted(diff_docs)})
    return vid, diff


# --- runs -----------------------------------------------------------------------

def start_run(submission_id: int, version_id: int, trigger: str) -> int:
    run_id = db.execute("INSERT INTO runs(submission_id, version_id, trigger, started_at) VALUES (?,?,?,?)",
                        (submission_id, version_id, trigger, clock.now_iso()))
    audit.log("system", "run_started", "run", run_id, {"submission_id": submission_id, "trigger": trigger})
    task = asyncio.get_running_loop().create_task(execute(run_id))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return run_id


def _load_context(run_id: int) -> RunContext:
    run = db.query_one("SELECT * FROM runs WHERE id = ?", (run_id,))
    version = db.query_one("SELECT * FROM submission_versions WHERE id = ?", (run["version_id"],))
    sub = db.query_one("SELECT * FROM submissions WHERE id = ?", (run["submission_id"],))
    ctx = RunContext(run_id=run_id, submission_id=run["submission_id"], version_id=run["version_id"], version_no=version["version_no"],
                     trigger=run["trigger"], form=db.loads(version["form_json"], {}))
    for d in db.query("SELECT * FROM documents WHERE version_id = ?", (run["version_id"],)):
        data = open(d["storage_path"], "rb").read()
        ctx.docs[d["slot"]] = DocInfo(slot=d["slot"], document_id=d["id"], filename=d["filename"], sha256=d["sha256"], mime="", size=d["size"], data=data)

    if run["trigger"] in ("resubmission", "manual_rerun", "bank_change"):
        prev = db.query_one("SELECT * FROM runs WHERE submission_id = ? AND id < ? AND finished_at IS NOT NULL ORDER BY id DESC LIMIT 1",
                            (run["submission_id"], run_id))
        if prev:
            prev_version = db.query_one("SELECT version_no FROM submission_versions WHERE id = ?", (prev["version_id"],))
            ctx.previous_version_no = prev_version["version_no"]
            for r in db.query("SELECT * FROM check_results WHERE run_id = ? ORDER BY id", (prev["id"],)):
                if r["check_name"] in checks.CHECKS:
                    ctx.previous[r["check_name"]] = CheckResult(
                        check=r["check_name"], stage=r["stage"], result=r["result"], severity=r["severity"], reason_code=r["reason_code"],
                        risk_points=r["risk_points"], explanation=r["explanation"], evidence=db.loads(r["evidence_json"], {}),
                        blocked_by=r["blocked_by"], required=bool(r["required"]), reused_from_run_id=prev["id"])
            diff = db.loads(version["diff_from_previous_json"], {})
            ctx.changed = {f"form:{k}" for k in diff.get("form", {})} | {f"doc:{k}" for k in diff.get("docs", {})}
            if run["trigger"] == "manual_rerun":
                ctx.changed = None  # a manual re-run repeats every check
        ctx.requested_items = [i["check"] for i in db.loads(sub["requested_items_json"], [])]
    return ctx


async def execute(run_id: int) -> None:
    ctx = _load_context(run_id)
    events.emit(run_id, "intake", "info", {"message": f"Run started ({ctx.trigger}, version {ctx.version_no}).", "stages": STAGE_INFO,
                                           "changed_inputs": sorted(ctx.changed) if ctx.changed is not None else None})
    for key, _, _, fn in STAGES:
        await run_stage(ctx, key, fn)
    finalise.emit_run_finished(ctx)


def recover_interrupted_runs() -> int:
    """Runs cut off by a restart are closed as manual review, never left hanging or approved."""
    rows = db.query("SELECT * FROM runs WHERE finished_at IS NULL")
    for r in rows:
        now = clock.now_iso()
        db.execute("UPDATE runs SET finished_at = ?, outcome_status = 'pending', outcome_route = 'manual_review' WHERE id = ?", (now, r["id"]))
        db.execute("UPDATE submissions SET status = 'pending', route = 'manual_review', top_reason_codes_json = ?, updated_at = ? WHERE id = ?",
                   (db.dumps(["CHECK_MISSING"]), now, r["submission_id"]))
        events.emit(r["id"], "finalise", "error", {"message": "The server restarted during this run. It was closed and sent to manual review."})
        events.emit(r["id"], "finalise", "run_finished", {"status": "pending", "route": "manual_review", "reason_codes": ["CHECK_MISSING"],
                                                          "submission_id": r["submission_id"]})
        audit.log("system", "run_interrupted", "run", r["id"], {})
    return len(rows)
