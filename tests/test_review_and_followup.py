"""Reviewer actions, vendor follow-up scheduling, resubmission via the portal, and bank change."""
import asyncio

import pytest
from fastapi.testclient import TestClient

from app import clock, db, review, scenarios, scheduler, settings_store
from app.main import app


def run(coro):
    return asyncio.run(coro)


def case_for(sub_id: int):
    return db.query_one("SELECT * FROM review_cases WHERE submission_id = ? ORDER BY id DESC LIMIT 1", (sub_id,))


def test_bank_collision_opens_priority_case_with_brief():
    r = run(scenarios.run("S3"))
    case = case_for(r.submission_id)
    assert case["state"] == "open" and case["priority"] == 1
    brief = db.query_one("SELECT * FROM outbox_emails WHERE run_id = ? AND kind = 'reviewer_brief'", (r.run_id,))
    assert brief["audience"] == "internal" and "BANK_COLLISION" in brief["body"] and brief["status"] == "draft"


def test_approve_requires_reason_and_records_everything():
    r = run(scenarios.run("S3"))
    case = case_for(r.submission_id)
    with pytest.raises(review.ReviewError):
        run(review.approve(case["id"], "  "))
    run(review.approve(case["id"], "Called both companies; shared treasury account confirmed by group CFO."))
    sub = db.query_one("SELECT * FROM submissions WHERE id = ?", (r.submission_id,))
    assert sub["status"] == "approved" and sub["vendor_id"]
    assert db.query_one("SELECT state FROM review_cases WHERE id = ?", (case["id"],))["state"] == "resolved"
    dec = db.query_one("SELECT * FROM decisions WHERE submission_id = ? ORDER BY id DESC LIMIT 1", (r.submission_id,))
    assert dec["decided_by"] == "reviewer:Demo Reviewer" and dec["status"] == "approved"
    assert db.query_one("SELECT 1 FROM audit_log WHERE action = 'review_approve' AND entity_id = ?", (str(case["id"]),))
    assert db.query_one("SELECT 1 FROM outbox_emails WHERE submission_id = ? AND kind = 'approval'", (r.submission_id,))
    with pytest.raises(review.ReviewError):  # resolved cases are closed for further decisions
        run(review.reject(case["id"], "changed my mind about this one"))


def test_flagged_content_needs_explicit_confirmation():
    r = run(scenarios.run("S5a"))
    case = case_for(r.submission_id)
    with pytest.raises(review.ReviewError, match="inspected the flagged content"):
        run(review.approve(case["id"], "Looks fine to me overall."))
    run(review.approve(case["id"], "Note was a test string from the vendor's template; documents verified by phone.", confirmed_flagged=True))
    assert db.query_one("SELECT status FROM submissions WHERE id = ?", (r.submission_id,))["status"] == "approved"


def test_reject_sends_neutral_closure():
    r = run(scenarios.run("S6d"))
    case = case_for(r.submission_id)
    run(review.reject(case["id"], "Same operators as the forged-certificate case in 2025."))
    email = db.query_one("SELECT * FROM outbox_emails WHERE submission_id = ? AND kind = 'closure'", (r.submission_id,))
    assert "unable to proceed" in email["body"] and "watchlist" not in email["body"].lower()


def test_clear_fuzzy_sanctions_hit_survives_rerun():
    r = run(scenarios.run("S6b"))
    case = case_for(r.submission_id)
    entry_id = db.loads(db.query_one("SELECT evidence_json FROM check_results WHERE run_id = ? AND check_name = 'sanctions'", (r.run_id,))["evidence_json"])["hits"][0]["entry_id"]
    review.clear_hit(case["id"], "sanctions", entry_id, "Different registered address and company number from the listed entity.")
    from app.pipeline import runner
    sub = db.query_one("SELECT * FROM submissions WHERE id = ?", (r.submission_id,))
    ver = db.query_one("SELECT id FROM submission_versions WHERE submission_id = ? AND version_no = ?", (sub["id"], sub["current_version"]))
    run_id = db.execute("INSERT INTO runs(submission_id, version_id, trigger, started_at) VALUES (?,?,'manual_rerun',?)", (sub["id"], ver["id"], clock.now_iso()))
    run(runner.execute(run_id))
    res = db.query_one("SELECT * FROM check_results WHERE run_id = ? AND check_name = 'sanctions'", (run_id,))
    assert res["result"] == "pass" and "previously_cleared" in res["evidence_json"]


def test_request_info_moves_case_to_vendor_followup():
    r = run(scenarios.run("S3"))
    case = case_for(r.submission_id)
    view = review.load_case(case["id"])
    key = next(o["key"] for o in review.request_options(view) if o["key"] == "reupload:bank_letter")
    run(review.request_info(case["id"], "Need a fresh bank letter before we can decide.", [key]))
    sub = db.query_one("SELECT * FROM submissions WHERE id = ?", (r.submission_id,))
    assert sub["route"] == "vendor_followup" and sub["resubmit_token"]
    email = db.query_one("SELECT * FROM outbox_emails WHERE submission_id = ? AND kind = 'request'", (r.submission_id,))
    assert "bank letter" in email["body"] and "collision" not in email["body"].lower()


def test_add_to_watchlist_from_case():
    r = run(scenarios.run("S3"))
    case = case_for(r.submission_id)
    view = review.load_case(case["id"])
    idents = [i for i in review.proposed_identifiers(view) if i[0] in ("bank_account", "tax_id")]
    wid = review.add_to_watchlist(case["id"], "Shared account with an existing vendor; monitor.", "high", idents, None)
    kinds = {row["kind"] for row in db.query("SELECT kind FROM watchlist_identifiers WHERE watchlist_id = ?", (wid,))}
    assert kinds == {"bank_account", "tax_id"}


def test_reminders_and_auto_close_follow_the_simulated_clock():
    r = run(scenarios.run("S1"))
    clock.advance(3)
    assert run(scheduler.tick()) == {"reminders": 1, "closed": 0}
    assert run(scheduler.tick()) == {"reminders": 0, "closed": 0}  # idempotent
    clock.advance(4)
    assert run(scheduler.tick())["reminders"] == 1
    clock.advance(8)
    assert run(scheduler.tick())["closed"] == 1
    sub = db.query_one("SELECT * FROM submissions WHERE id = ?", (r.submission_id,))
    assert sub["status"] == "rejected" and sub["closed_reason"] == "auto_closed_no_response"
    kinds = [e["kind"] for e in db.query("SELECT kind FROM outbox_emails WHERE submission_id = ? ORDER BY id", (r.submission_id,))]
    assert kinds == ["request", "reminder", "reminder", "auto_close"]


def test_portal_resubmission_end_to_end():
    r = run(scenarios.run("S1"))
    token = db.query_one("SELECT resubmit_token FROM submissions WHERE id = ?", (r.submission_id,))["resubmit_token"]
    good_cert = (scenarios.SAMPLES / "S7a" / "tax_cert_v2.pdf").read_bytes()
    with TestClient(app) as client:
        page = client.get(f"/resubmit/{token}").text
        assert "What we need from you" in page and "GST registration certificate" in page
        resp = client.post(f"/resubmit/{token}", files={"tax_cert": ("gst.pdf", good_cert, "application/pdf")}, follow_redirects=False)
        assert resp.status_code == 303
        run_id = int(resp.headers["location"].split("sent=")[1])
        for _ in range(200):
            if db.query_one("SELECT finished_at FROM runs WHERE id = ?", (run_id,))["finished_at"]:
                break
            client.get("/healthz")
            asyncio.run(asyncio.sleep(0.05))
    sub = db.query_one("SELECT * FROM submissions WHERE id = ?", (r.submission_id,))
    assert sub["current_version"] == 2 and sub["status"] == "approved"


def test_bank_change_puts_payments_on_hold_with_callback_task():
    r = run(scenarios.run("S14"))
    sub = db.query_one("SELECT * FROM submissions WHERE id = ?", (r.submission_id,))
    vendor = db.query_one("SELECT * FROM vendors WHERE id = ?", (sub["vendor_id"],))
    assert vendor["payment_hold"] == 1
    case = case_for(r.submission_id)
    tasks = db.loads(case["tasks_json"], [])
    assert any("phone number already on file" in t for t in tasks)
    run(review.approve(case["id"], "Called the number on file; CFO confirmed the new account."))
    vendor = db.query_one("SELECT * FROM vendors WHERE id = ?", (sub["vendor_id"],))
    assert vendor["payment_hold"] == 0 and vendor["bank_account"] == "50100975318642"


def test_review_pages_render():
    r = run(scenarios.run("S5b"))
    case = case_for(r.submission_id)
    with TestClient(app) as client:
        html = client.get(f"/review/{case['id']}").text
        assert "Flagged content" in html and "hidden text, page 1" in html
        assert "I have inspected the flagged content" in html
        assert client.get("/review").status_code == 200 and client.get("/awaiting").status_code == 200
        doc = db.query_one("SELECT d.id FROM documents d JOIN submission_versions v ON v.id = d.version_id WHERE v.submission_id = ? LIMIT 1", (r.submission_id,))
        img = client.get(f"/documents/{doc['id']}/page/1.png")
        assert img.status_code == 200 and img.content.startswith(b"\x89PNG")
