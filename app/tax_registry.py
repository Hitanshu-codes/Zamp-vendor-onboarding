"""The tax-ID rule registry: lookup, storing learned rules, promotion, and admin actions.

Status lifecycle: seed / verified (trusted) · provisional (learned from the web, not yet trusted)
· disabled (never used). Provisional rules are promoted by a reviewer, or automatically after
RULE_PROMOTE_AFTER_N_APPROVED distinct vendors whose IDs matched them are approved.
"""
from __future__ import annotations

from datetime import date

from app import audit, clock, db
from app.config import config
from app.domain.rule_selftest import SelfTestReport, self_test
from app.domain.taxids import CHECKSUMS, TaxRule

STATUS_ORDER = {"verified": 0, "seed": 1, "provisional": 2}


def find_rule(country: str, tax_type: str) -> TaxRule | None:
    rows = db.query("SELECT * FROM tax_rules WHERE country = ? AND tax_type = ? AND status != 'disabled'", (country, tax_type))
    if not rows:
        return None
    return TaxRule.from_row(min(rows, key=lambda r: (STATUS_ORDER.get(r["status"], 9), -r["id"])))


def all_active_rules() -> list[TaxRule]:
    return [TaxRule.from_row(r) for r in db.query("SELECT * FROM tax_rules WHERE status != 'disabled'")]


def reverify_due(row) -> bool:
    last = (row["last_verified_at"] or row["created_at"])[:10]
    return (clock.today() - date.fromisoformat(last)).days > config.rule_reverify_days


def store_learned(country: str, tax_type: str, description: str, regex: str, example: str, source_url: str,
                  checksum_algo: str | None, notes: str) -> TaxRule:
    now = clock.now_iso()
    algo = checksum_algo if checksum_algo in CHECKSUMS else None  # never execute anything from the web
    rid = db.execute(
        "INSERT INTO tax_rules(country, tax_type, description, regex, checksum_algo, example, source_url, status, last_verified_at,"
        " created_at, created_by, notes) VALUES (?,?,?,?,?,?,?, 'provisional', ?,?, 'search', ?)",
        (country, tax_type, description[:500], regex, algo, example, source_url, now, now, notes[:500]),
    )
    audit.log("system", "tax_rule_learned", "tax_rule", rid, {"country": country, "tax_type": tax_type, "regex": regex, "source": source_url})
    return TaxRule.from_row(db.query_one("SELECT * FROM tax_rules WHERE id = ?", (rid,)))


def confirm_on_approval(submission_id: int) -> None:
    """Count an approved vendor towards promoting the provisional rule its tax ID matched."""
    row = db.query_one(
        "SELECT cr.evidence_json FROM check_results cr JOIN runs r ON r.id = cr.run_id WHERE r.submission_id = ? AND cr.check_name = 'tax_rule'"
        " ORDER BY cr.id DESC LIMIT 1", (submission_id,))
    if row is None:
        return
    rule_id = db.loads(row["evidence_json"], {}).get("rule_id")
    rule = db.query_one("SELECT * FROM tax_rules WHERE id = ?", (rule_id,)) if rule_id else None
    if rule is None or rule["status"] != "provisional":
        return
    sub = db.query_one("SELECT vendor_name FROM submissions WHERE id = ?", (submission_id,))
    refs = db.loads(rule["confirmed_vendor_refs_json"], [])
    ref = f"submission:{submission_id}:{(sub['vendor_name'] or '')[:60]}"
    if any(r.startswith(f"submission:{submission_id}:") for r in refs):
        return
    refs.append(ref)
    db.execute("UPDATE tax_rules SET times_confirmed = ?, confirmed_vendor_refs_json = ? WHERE id = ?", (len(refs), db.dumps(refs), rule_id))
    if len(refs) >= config.rule_promote_after_n_approved:
        db.execute("UPDATE tax_rules SET status = 'verified', last_verified_at = ? WHERE id = ?", (clock.now_iso(), rule_id))
        audit.log("system", "tax_rule_promoted", "tax_rule", rule_id, {"approved_vendors": len(refs)})


# --- admin actions (from the Tax rules page; audited) ------------------------------------------

def _get(rule_id: int):
    row = db.query_one("SELECT * FROM tax_rules WHERE id = ?", (rule_id,))
    if row is None:
        raise ValueError("Rule not found.")
    return row


def verify(rule_id: int, actor: str, reason: str) -> None:
    _get(rule_id)
    db.execute("UPDATE tax_rules SET status = 'verified', last_verified_at = ? WHERE id = ?", (clock.now_iso(), rule_id))
    audit.log(actor, "tax_rule_verified", "tax_rule", rule_id, {"reason": reason})


def disable(rule_id: int, actor: str, reason: str) -> None:
    _get(rule_id)
    db.execute("UPDATE tax_rules SET status = 'disabled' WHERE id = ?", (rule_id,))
    audit.log(actor, "tax_rule_disabled", "tax_rule", rule_id, {"reason": reason})


def enable(rule_id: int, actor: str, reason: str) -> None:
    row = _get(rule_id)
    status = "seed" if row["created_by"] == "seed" and row["notes"] is None else "provisional"
    db.execute("UPDATE tax_rules SET status = ? WHERE id = ?", (status, rule_id))
    audit.log(actor, "tax_rule_enabled", "tax_rule", rule_id, {"reason": reason, "status": status})


def edit(rule_id: int, actor: str, reason: str, regex: str, example: str, description: str) -> SelfTestReport:
    """A reviewer's edited regex must pass the same self-test as a learned one."""
    row = _get(rule_id)
    report = self_test(regex, [e.strip() for e in example.split(",") if e.strip()])
    if report.passed:
        db.execute("UPDATE tax_rules SET regex = ?, example = ?, description = ?, last_verified_at = ? WHERE id = ?",
                   (regex, example, description or row["description"], clock.now_iso(), rule_id))
        audit.log(actor, "tax_rule_edited", "tax_rule", rule_id, {"reason": reason, "old": row["regex"], "new": regex})
    return report


def forget(rule_id: int, actor: str) -> None:
    """Demo reset of one learned rule, so first-seen learning can be shown again."""
    row = _get(rule_id)
    if row["created_by"] != "search":
        raise ValueError("Only rules learned from the web can be forgotten.")
    db.execute("DELETE FROM tax_rules WHERE id = ?", (rule_id,))
    db.execute("DELETE FROM tax_rule_search_log WHERE country = ? AND tax_type = ?", (row["country"], row["tax_type"]))
    audit.log(actor, "tax_rule_forgotten", "tax_rule", rule_id, {"country": row["country"], "tax_type": row["tax_type"]})
