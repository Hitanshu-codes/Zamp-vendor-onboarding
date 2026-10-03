"""Dashboard: KPIs, filterable history, charts, submission detail (drawer + page) and JSON export."""
from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from app import db
from app.decision import reason_codes
from app.pipeline import forms
from app.pipeline.checks import SLOT_LABEL, SLOTS
from app.web.render import page, templates
from app.web.review_routes import evidence_grid

router = APIRouter()

BUCKETS = [  # (key, label, colour token) for status charts; colour always paired with a text label
    ("approved", "Approved", "s-ok"),
    ("vendor_followup", "Waiting on vendor", "s-warn"),
    ("manual_review", "Manual review", "s-review"),
    ("rejected", "Rejected", "s-bad"),
]


def bucket(sub) -> str:
    if sub["status"] == "approved":
        return "approved"
    if sub["status"] == "rejected":
        return "rejected"
    return "manual_review" if sub["route"] == "manual_review" else "vendor_followup"


def _latest_decisions() -> dict[int, dict]:
    rows = db.query("SELECT d.* FROM decisions d JOIN (SELECT submission_id, MAX(id) mid FROM decisions GROUP BY submission_id) x ON x.mid = d.id")
    return {r["submission_id"]: dict(r) for r in rows}


def _fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    if seconds < 60:
        return f"{seconds:.0f} s"
    if seconds < 3600:
        return f"{seconds / 60:.0f} min"
    if seconds < 86400:
        return f"{seconds / 3600:.1f} h"
    return f"{seconds / 86400:.1f} days"


def kpis(subs: list, latest: dict[int, dict]) -> dict:
    counts = Counter(bucket(s) for s in subs)
    # Auto-decided: the current outcome came from the rules engine without a person (approved, rejected, or sent back to the vendor).
    auto = sum(1 for s in subs if bucket(s) != "manual_review" and latest.get(s["id"], {}).get("decided_by", "engine") == "engine")
    first_final: dict[int, str] = {}
    for d in db.query("SELECT submission_id, ts FROM decisions WHERE status IN ('approved','rejected') ORDER BY id"):
        first_final.setdefault(d["submission_id"], d["ts"])
    durations = []
    for s in subs:
        if s["id"] in first_final:
            durations.append((datetime.fromisoformat(first_final[s["id"]]) - datetime.fromisoformat(s["created_at"])).total_seconds())
    tax_rows = db.query("SELECT evidence_json FROM check_results WHERE check_name = 'tax_rule' AND reused_from_run_id IS NULL")
    caches = [db.loads(r["evidence_json"], {}).get("cache") for r in tax_rows]
    lookups = [c for c in caches if c]
    return {
        "total": len(subs),
        "auto_pct": round(100 * auto / len(subs)) if subs else 0,
        "counts": {k: counts.get(k, 0) for k, _, _ in BUCKETS},
        "median_decision": _fmt_duration(statistics.median(durations) if durations else None),
        "cache_hit_pct": round(100 * sum(1 for c in lookups if c == "hit") / len(lookups)) if lookups else None,
        "cache_lookups": len(lookups),
    }


def _codes_for(sub_id: int, latest: dict[int, dict]) -> list[str]:
    d = latest.get(sub_id)
    if not d:
        return []
    return list(dict.fromkeys(c["code"] for c in db.loads(d["reason_codes_json"], []) if c.get("code")))


@router.get("/dashboard")
def dashboard(request: Request, q: str = "", status: str = "", country: str = "", code: str = "", date_from: str = "", date_to: str = ""):
    all_subs = db.query("SELECT * FROM submissions ORDER BY updated_at DESC")
    latest = _latest_decisions()
    k = kpis(all_subs, latest)

    def keep(s) -> bool:
        if q and q.lower() not in (s["vendor_name"] or "").lower() and q != str(s["id"]):
            return False
        if status and bucket(s) != status:
            return False
        if country and s["country"] != country:
            return False
        if code and code not in _codes_for(s["id"], latest):
            return False
        if date_from and s["created_at"][:10] < date_from:
            return False
        if date_to and s["created_at"][:10] > date_to:
            return False
        return True

    rows = [dict(s) | {"bucket": bucket(s), "codes": _codes_for(s["id"], latest)} for s in all_subs if keep(s)]

    code_counts = Counter(c for s in rows for c in s["codes"])
    top_codes = [{"code": c, "n": n, "severity": reason_codes.CATALOGUE[c].severity if c in reason_codes.CATALOGUE else "info",
                  "meaning": reason_codes.CATALOGUE[c].meaning if c in reason_codes.CATALOGUE else ""} for c, n in code_counts.most_common(8)]
    by_day: dict[str, Counter] = defaultdict(Counter)
    for s in rows:
        by_day[s["created_at"][:10]][s["bucket"]] += 1
    days = [{"day": d, "counts": {b: by_day[d].get(b, 0) for b, _, _ in BUCKETS}, "total": sum(by_day[d].values())} for d in sorted(by_day)][-14:]
    countries = sorted({s["country"] for s in all_subs if s["country"]})
    all_codes = sorted({c for s in all_subs for c in _codes_for(s["id"], latest)})
    return page(request, "dashboard.html", "dashboard", k=k, rows=rows, top_codes=top_codes, days=days, buckets=BUCKETS,
                filters={"q": q, "status": status, "country": country, "code": code, "date_from": date_from, "date_to": date_to},
                countries=countries, all_codes=all_codes, country_names=forms.COUNTRIES,
                max_code=max([t["n"] for t in top_codes] or [1]), max_day=max([d["total"] for d in days] or [1]))


# --- submission detail ---------------------------------------------------------------------

def submission_detail(sub_id: int) -> dict:
    sub = db.query_one("SELECT * FROM submissions WHERE id = ?", (sub_id,))
    if sub is None:
        raise HTTPException(404, "submission not found")
    versions = db.query("SELECT * FROM submission_versions WHERE submission_id = ? ORDER BY version_no", (sub_id,))
    current = versions[-1]
    form = db.loads(current["form_json"], {})
    runs = db.query("SELECT r.*, v.version_no FROM runs r JOIN submission_versions v ON v.id = r.version_id WHERE r.submission_id = ? ORDER BY r.id", (sub_id,))
    last_run = runs[-1] if runs else None
    checks = db.query("SELECT * FROM check_results WHERE run_id = ? ORDER BY id", (last_run["id"],)) if last_run else []
    decisions = db.query("SELECT * FROM decisions WHERE submission_id = ? ORDER BY id", (sub_id,))
    docs_by_version = {}
    for v in versions:
        docs_by_version[v["version_no"]] = {d["slot"]: dict(d) for d in db.query("SELECT * FROM documents WHERE version_id = ?", (v["id"],))}
    emails = db.query("SELECT * FROM outbox_emails WHERE submission_id = ? ORDER BY id", (sub_id,))
    cases = db.query("SELECT * FROM review_cases WHERE submission_id = ? ORDER BY id", (sub_id,))
    run_ids = [str(r["id"]) for r in runs]
    case_ids = [str(c["id"]) for c in cases]
    email_ids = [str(e["id"]) for e in emails]
    audit_rows = db.query(
        "SELECT * FROM audit_log WHERE (entity_type = 'submission' AND entity_id = ?)"
        f" OR (entity_type = 'run' AND entity_id IN ({','.join('?' * len(run_ids)) or 'NULL'}))"
        f" OR (entity_type = 'review_case' AND entity_id IN ({','.join('?' * len(case_ids)) or 'NULL'}))"
        f" OR (entity_type = 'outbox_email' AND entity_id IN ({','.join('?' * len(email_ids)) or 'NULL'}))"
        f" OR (entity_type = 'llm' AND CAST(json_extract(detail_json, '$.run_id') AS TEXT) IN ({','.join('?' * len(run_ids)) or 'NULL'}))"
        " OR (entity_type = 'vendor' AND entity_id = ?) ORDER BY id",
        (str(sub_id), *run_ids, *case_ids, *email_ids, *run_ids, str(sub["vendor_id"] or "")))
    return {"sub": dict(sub), "form": form, "versions": versions, "runs": runs, "last_run": last_run, "checks": checks,
            "decisions": decisions, "docs_by_version": docs_by_version, "current_docs": docs_by_version[current["version_no"]],
            "emails": emails, "cases": cases, "audit": audit_rows, "bucket": bucket(sub)}


def _ctx(sub_id: int) -> dict:
    d = submission_detail(sub_id)
    order = {"fail": 0, "warn": 1, "pass": 2, "skipped": 3}
    d["checks"] = sorted(d["checks"], key=lambda c: (order.get(c["result"], 9), c["id"]))  # problems first
    d.update({"grid": evidence_grid(d["form"], d["current_docs"]), "slots": [(s, SLOT_LABEL[s]) for s in SLOTS], "loads": db.loads,
              "labels": forms.LABELS, "buckets": {k: (label, cls) for k, label, cls in BUCKETS}})
    return d


@router.get("/submissions/{sub_id}/drawer")
def drawer(request: Request, sub_id: int):
    return templates.TemplateResponse(request, "_submission_detail.html", _ctx(sub_id) | {"partial": True})


@router.get("/submissions/{sub_id}")
def submission_page(request: Request, sub_id: int):
    return page(request, "submission.html", "dashboard", **(_ctx(sub_id) | {"partial": False}))


@router.get("/submissions/{sub_id}/export.json")
def export(sub_id: int):
    d = submission_detail(sub_id)

    def rows(xs):
        return [dict(x) for x in xs]

    payload = {
        "submission": d["sub"], "current_form": d["form"],
        "versions": [dict(v) | {"form": db.loads(v["form_json"], {}), "diff": db.loads(v["diff_from_previous_json"], {})} for v in d["versions"]],
        "runs": rows(d["runs"]),
        "latest_checks": [dict(c) | {"evidence": db.loads(c["evidence_json"], {})} for c in d["checks"]],
        "decisions": [dict(x) | {"reason_codes": db.loads(x["reason_codes_json"], [])} for x in d["decisions"]],
        "documents": {v: {s: {k: doc[k] for k in ("filename", "sha256", "size", "classified_type", "extraction_quality")}
                          | {"extraction": db.loads(doc["extraction_json"], None)} for s, doc in docs.items()} for v, docs in d["docs_by_version"].items()},
        "emails": rows(d["emails"]), "review_cases": rows(d["cases"]), "audit_trail": rows(d["audit"]),
    }
    for v in payload["versions"]:
        v.pop("form_json", None)
        v.pop("diff_from_previous_json", None)
    return JSONResponse(payload, headers={"Content-Disposition": f'attachment; filename="submission-{sub_id}.json"'})
