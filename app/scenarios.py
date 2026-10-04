"""Run a sample scenario through the real pipeline and compare it with expected.json (used by tests and scripts)."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from app import clock, db, settings_store
from app.config import ROOT
from app.pipeline import runner

SAMPLES = ROOT / "samples"
ORDER = ["S0", "S1", "S2", "S3", "S4a", "S4b", "S4c", "S5a", "S5b", "S5c", "S5d", "S6a", "S6b", "S6c", "S6d", "S7a", "S7b",
         "S8", "S9", "S10", "S11", "S12", "S13", "S14", "S15", "S16", "S17", "S18", "S19"]


@dataclass
class ScenarioResult:
    id: str
    ok: bool
    status: str
    route: str
    codes: list[str]
    expected: dict
    run_id: int
    submission_id: int

    def line(self) -> str:
        e = self.expected
        return (f"{'PASS' if self.ok else 'FAIL'} {self.id:4s} got {self.status}/{self.route} codes={self.codes} | "
                f"expected {e['status']}/{e['route']} {e['codes']}")


def available() -> list[str]:
    return [s for s in ORDER if (SAMPLES / s / "expected.json").exists()]


def load(sid: str) -> tuple[dict, dict[str, tuple[str, bytes]], dict]:
    folder = SAMPLES / sid
    form = json.loads((folder / "form.json").read_text(encoding="utf-8"))
    files = {p.stem: (p.name, p.read_bytes()) for p in sorted(folder.iterdir())
             if p.suffix in (".pdf", ".png", ".jpg") and not p.stem.endswith("_v2")}
    meta = json.loads((folder / "expected.json").read_text(encoding="utf-8"))
    return form, files, meta


def _run_now(sub_id: int, ver_id: int, trigger: str) -> int:
    return db.execute("INSERT INTO runs(submission_id, version_id, trigger, started_at) VALUES (?,?,?,?)",
                      (sub_id, ver_id, trigger, clock.now_iso()))


async def run(sid: str) -> ScenarioResult:
    """Run one scenario; its settings (e.g. kill switch) apply only for the duration of the scenario."""
    form, files, meta = load(sid)
    saved = {k: settings_store.get(k) for k in meta.get("settings", {})}
    for k, v in meta.get("settings", {}).items():
        settings_store.set_(k, v)
    try:
        return await _run(sid, form, files, meta)
    finally:
        for k, v in saved.items():
            settings_store.set_(k, v)


async def _run(sid: str, form: dict, files: dict, meta: dict) -> ScenarioResult:
    sub_id, ver_id = runner.create_submission(form, files)
    run_id = _run_now(sub_id, ver_id, "initial")
    await runner.execute(run_id)
    if meta.get("resubmission"):
        r = meta["resubmission"]
        trigger = r.get("trigger", "resubmission")
        new_files = {slot: (f"{slot}.pdf", (SAMPLES / sid / f"{slot}_v2.pdf").read_bytes()) for slot in r["replace_docs"]}
        if trigger == "bank_change":
            vendor_id = db.query_one("SELECT vendor_id FROM submissions WHERE id = ?", (sub_id,))["vendor_id"]
            db.execute("UPDATE vendors SET payment_hold = 1 WHERE id = ?", (vendor_id,))
        ver2, _ = runner.create_version(sub_id, r["form_changes"], new_files)
        run_id = _run_now(sub_id, ver2, trigger)
        await runner.execute(run_id)
    sub = db.query_one("SELECT * FROM submissions WHERE id = ?", (sub_id,))
    dec = db.query_one("SELECT * FROM decisions WHERE run_id = ?", (run_id,))
    codes = [t["code"] for t in db.loads(dec["reason_codes_json"], [])] if dec else []
    exp = meta["expected"]
    ok = sub["status"] == exp["status"] and sub["route"] == exp["route"] and all(c in codes for c in exp["codes"])
    if exp.get("codes_any"):
        ok = ok and any(c in codes for c in exp["codes_any"])
    if exp["status"] == "approved":
        ok = ok and all(c in ("FREE_EMAIL_DOMAIN", "DEGRADED_DOCUMENT") for c in codes)
    return ScenarioResult(sid, ok, sub["status"], sub["route"], codes, exp, run_id, sub_id)
