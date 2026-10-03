"""Reviewer actions on manual-review cases. Every action needs a typed reason and is written to
review_actions and the append-only audit log."""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import timedelta

from app import audit, clock, db, settings_store
from app.comms import outbox
from app.comms.asks import ask_for
from app.config import config
from app.domain.banking import normalize_account
from app.domain.identifiers import normalize_identifier
from app.pipeline import forms
from app.pipeline.checks import SLOT_LABEL, SLOTS
from app.pipeline.stages.finalise import add_vendor

FLAGGED_CODES = {"INJECTION_DETECTED", "SANCTIONS_FUZZY", "SANCTIONS_EXACT"}
WATCHLIST_KINDS = ("name", "bank_account", "tax_id", "email_domain", "phone", "address", "director")


class ReviewError(ValueError):
    pass


@dataclass
class CaseView:
    case: dict
    submission: dict
    form: dict
    codes: list[str]


def reviewer() -> str:
    return settings_store.get("reviewer_name")


def load_case(case_id: int) -> CaseView:
    case = db.query_one("SELECT * FROM review_cases WHERE id = ?", (case_id,))
    if case is None:
        raise ReviewError("Case not found.")
    sub = db.query_one("SELECT * FROM submissions WHERE id = ?", (case["submission_id"],))
    version = db.query_one("SELECT * FROM submission_versions WHERE submission_id = ? AND version_no = ?", (sub["id"], sub["current_version"]))
    codes = [r["code"] for r in db.loads(case["reasons_json"], [])]
    return CaseView(dict(case), dict(sub), db.loads(version["form_json"], {}), codes)


def needs_flag_confirmation(view: CaseView) -> bool:
    return bool(set(view.codes) & FLAGGED_CODES)


def _require_open(view: CaseView) -> None:
    if view.case["state"] != "open":
        raise ReviewError("This case is already resolved.")


def _reason(text: str) -> str:
    text = " ".join((text or "").split())
    if len(text) < 5:
        raise ReviewError("Please type a reason (at least a few words). It is stored in the audit trail.")
    return text[:1000]


def _record(view: CaseView, action: str, reason: str, payload: dict | None = None) -> None:
    name = reviewer()
    db.execute("INSERT INTO review_actions(case_id, ts, reviewer, action, reason_text, payload_json) VALUES (?,?,?,?,?,?)",
               (view.case["id"], clock.now_iso(), name, action, reason, db.dumps(payload or {})))
    audit.log(f"reviewer:{name}", f"review_{action}", "review_case", view.case["id"],
              {"submission_id": view.submission["id"], "reason": reason, **(payload or {})})


def _decide(view: CaseView, status: str, route: str, codes: list[str], rationale: str) -> None:
    name = reviewer()
    now = clock.now_iso()
    db.execute("INSERT INTO decisions(run_id, submission_id, ts, status, route, reason_codes_json, risk_points, rationale_text, decided_by)"
               " VALUES (?,?,?,?,?,?,?,?,?)",
               (view.case["run_id"], view.submission["id"], now, status, route, db.dumps([{"code": c} for c in codes]),
                view.submission["risk_points"], rationale, f"reviewer:{name}"))
    db.execute("UPDATE submissions SET status = ?, route = ?, updated_at = ? WHERE id = ?", (status, route, now, view.submission["id"]))


def _resolve(view: CaseView) -> None:
    db.execute("UPDATE review_cases SET state = 'resolved', assignee = ? WHERE id = ?", (reviewer(), view.case["id"]))


class _Ctx:
    """Minimal stand-in for a RunContext when sending emails outside a run."""

    def __init__(self, view: CaseView):
        self.submission_id = view.submission["id"]
        self.run_id = view.case["run_id"]
        self.form = view.form


def _is_bank_change(view: CaseView) -> bool:
    run = db.query_one("SELECT trigger FROM runs WHERE id = ?", (view.case["run_id"],))
    return bool(run and run["trigger"] == "bank_change")


async def approve(case_id: int, reason: str, confirmed_flagged: bool = False) -> None:
    view = load_case(case_id)
    _require_open(view)
    reason = _reason(reason)
    if needs_flag_confirmation(view) and not confirmed_flagged:
        raise ReviewError("This case contains flagged content (injection or sanctions). Tick 'I have inspected the flagged content' to approve.")
    _record(view, "approve", reason, {"codes": view.codes, "confirmed_flagged": confirmed_flagged})
    _decide(view, "approved", "none", view.codes,
            f"Approved by reviewer {reviewer()} after manual review of {', '.join(view.codes) or 'the case'}. "
            "The reviewer's reason is recorded in the audit trail.")
    if _is_bank_change(view):
        vendor_id = view.submission["vendor_id"]
        db.execute("UPDATE vendors SET bank_account = ?, bank_code = ?, payment_hold = 0 WHERE id = ?",
                   (normalize_account(forms.account(view.form)), view.form.get("bank_code", ""), vendor_id))
        audit.log(f"reviewer:{reviewer()}", "bank_details_changed", "vendor", vendor_id, {"payment_hold": False})
    else:
        add_vendor(view.submission["id"], view.form)
    _resolve(view)
    await outbox.send_vendor_email(_Ctx(view), "approval", "vendor_approval.txt", [])


async def reject(case_id: int, reason: str) -> None:
    view = load_case(case_id)
    _require_open(view)
    reason = _reason(reason)
    _record(view, "reject", reason, {"codes": view.codes})
    _decide(view, "rejected", "none", view.codes,
            f"Rejected by reviewer {reviewer()} after manual review of {', '.join(view.codes) or 'the case'}. "
            "The reviewer's reason is recorded in the audit trail.")
    if _is_bank_change(view):
        # The existing, verified bank details stay; the hold is lifted.
        db.execute("UPDATE vendors SET payment_hold = 0 WHERE id = ?", (view.submission["vendor_id"],))
        db.execute("UPDATE submissions SET status = 'approved' WHERE id = ?", (view.submission["id"],))
    _resolve(view)
    await outbox.send_vendor_email(_Ctx(view), "closure", "vendor_closure.txt", view.codes)


def request_options(view: CaseView) -> list[dict]:
    """What a reviewer may ask the vendor for: the case's fixable findings plus a re-upload of any document."""
    options: list[dict] = []
    for r in db.query("SELECT * FROM check_results WHERE run_id = ? AND result = 'fail' AND severity = 'fixable'", (view.case["run_id"],)):
        ev = db.loads(r["evidence_json"], {})
        options.append({"key": f"check:{r['check_name']}", "code": r["reason_code"], "check": r["check_name"],
                        "text": ask_for(r["reason_code"], r["check_name"], ev, view.form.get("country", ""))})
    for slot in SLOTS:
        options.append({"key": f"reupload:{slot}", "code": "DOC_UNREADABLE", "check": f"doc_type.{slot}",
                        "text": f"Please upload a fresh, clear copy of your {SLOT_LABEL[slot]}."})
    options.append({"key": "fix:bank", "code": "BANK_DETAILS_INVALID", "check": "bank_format",
                    "text": "Please re-check and re-enter your bank account number and bank code."})
    return options


async def request_info(case_id: int, reason: str, keys: list[str]) -> None:
    view = load_case(case_id)
    _require_open(view)
    reason = _reason(reason)
    chosen = [o for o in request_options(view) if o["key"] in set(keys)]
    if not chosen:
        raise ReviewError("Choose at least one item to request from the vendor.")
    _record(view, "request_info", reason, {"items": [o["key"] for o in chosen]})
    token = view.submission["resubmit_token"] or secrets.token_urlsafe(18)
    now = clock.now()
    due = (now + timedelta(days=config.vendor_response_due_days)).date().isoformat()
    items = [{"code": o["code"], "check": o["check"], "text": o["text"]} for o in chosen]
    db.execute("UPDATE submissions SET status = 'pending', route = 'vendor_followup', resubmit_token = ?, requested_items_json = ?,"
               " requested_at = ?, due_at = ?, reminders_sent = 0, updated_at = ? WHERE id = ?",
               (token, db.dumps(items), now.isoformat(timespec="seconds"), due, now.isoformat(timespec="seconds"), view.submission["id"]))
    _resolve(view)
    await outbox.send_vendor_email(_Ctx(view), "request", "vendor_request.txt", [o["code"] for o in chosen],
                                   items=[o["text"] for o in chosen], due_date=due, resubmit_url=outbox.resubmit_url(token))


def clear_hit(case_id: int, list_name: str, entry_id: int, reason: str) -> None:
    view = load_case(case_id)
    _require_open(view)
    reason = _reason(reason)
    if list_name not in ("watchlist", "sanctions"):
        raise ReviewError("Unknown list.")
    if list_name == "sanctions":
        hit = db.query_one("SELECT evidence_json FROM check_results WHERE run_id = ? AND check_name = 'sanctions'", (view.case["run_id"],))
        hits = db.loads(hit["evidence_json"], {}).get("hits", []) if hit else []
        if any(h.get("entry_id") == entry_id and h.get("match_type") == "exact" for h in hits):
            raise ReviewError("Exact sanctions matches cannot be cleared.")
    else:
        db.execute("UPDATE watchlist_matches SET cleared_by = ?, cleared_reason = ? WHERE run_id = ? AND watchlist_id = ?",
                   (reviewer(), reason, view.case["run_id"], entry_id))
    _record(view, "clear_hit", reason, {"list": list_name, "entry_id": entry_id})


def proposed_identifiers(view: CaseView) -> list[tuple[str, str]]:
    """Every identifier from the case, offered as checkboxes when adding to the watchlist."""
    f = view.form
    out = [("name", f.get("legal_name", "")), ("bank_account", forms.account(f)), ("tax_id", f.get("tax_id", "")),
           ("email_domain", f.get("contact_email", "").rsplit("@", 1)[-1]), ("phone", f.get("contact_phone", "")),
           ("address", f.get("registered_address", ""))]
    for d in db.query("SELECT extraction_json FROM documents d JOIN submission_versions v ON v.id = d.version_id "
                      "WHERE v.submission_id = ? AND v.version_no = ?", (view.submission["id"], view.submission["current_version"])):
        ex = db.loads(d["extraction_json"], {}) or {}
        out += [("director", x) for x in ex.get("directors_or_owners", [])]
    seen, unique = set(), []
    for kind, value in out:
        if value and (kind, value) not in seen:
            seen.add((kind, value))
            unique.append((kind, value))
    return unique


def add_to_watchlist(case_id: int, reason: str, severity: str, identifiers: list[tuple[str, str]], expires_at: str | None) -> int:
    view = load_case(case_id)
    reason = _reason(reason)
    if severity not in ("blocked", "high", "watch"):
        raise ReviewError("Choose a severity.")
    identifiers = [(k, v) for k, v in identifiers if k in WATCHLIST_KINDS and v]
    if not identifiers:
        raise ReviewError("Choose at least one identifier.")
    label = f"{view.form.get('legal_name', 'Unknown')[:80]} (from case #{case_id})"
    wid = db.execute("INSERT INTO watchlist(label, severity, reason, source_submission_id, added_by, added_at, expires_at, active)"
                     " VALUES (?,?,?,?,?,?,?,1)",
                     (label, severity, reason, view.submission["id"], f"reviewer:{reviewer()}", clock.now_iso(), expires_at or None))
    for kind, value in identifiers:
        db.execute("INSERT INTO watchlist_identifiers(watchlist_id, kind, value_normalized, value_display) VALUES (?,?,?,?)",
                   (wid, kind, normalize_identifier(kind, value), value[:200]))
    _record(view, "add_watchlist", reason, {"watchlist_id": wid, "severity": severity, "identifiers": [k for k, _ in identifiers]})
    return wid


def history(case_id: int) -> list:
    return db.query("SELECT * FROM review_actions WHERE case_id = ? ORDER BY id", (case_id,))

