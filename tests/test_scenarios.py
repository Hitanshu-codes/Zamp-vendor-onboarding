"""Every sample scenario through the real pipeline in replay mode (recorded LLM responses, no network)."""
import asyncio

import pytest
from fastapi.testclient import TestClient

from app import db, scenarios, settings_store
from app.main import app

# Scenarios that need the tax-rule web search are added once their recordings/fixtures exist.
NEEDS_SEARCH: set[str] = set()  # S4b/S4c depend on S4a; run as a sequence below
DEPENDS_ON_S4A = {"S4b", "S4c"}
IDS = [s for s in scenarios.available() if s not in NEEDS_SEARCH]


@pytest.mark.parametrize("sid", IDS)
def test_scenario_matches_expected(sid):
    settings_store.set_("llm_mode", "replay")
    if sid in DEPENDS_ON_S4A:
        asyncio.run(scenarios.run("S4a"))  # the US rule must be learned first
    r = asyncio.run(scenarios.run(sid))
    assert r.ok, r.line()


def test_resubmission_reuses_unaffected_checks():
    r = asyncio.run(scenarios.run("S7a"))
    reused = db.query_one("SELECT COUNT(*) c FROM check_results WHERE run_id = ? AND reused_from_run_id IS NOT NULL", (r.run_id,))["c"]
    rerun = {row["check_name"] for row in db.query("SELECT check_name FROM check_results WHERE run_id = ? AND reused_from_run_id IS NULL", (r.run_id,))}
    assert reused >= 10
    assert "doc_type.tax_cert" in rerun and "name.tax_cert" in rerun  # failed / dependent checks re-ran
    assert "bank_format" not in rerun  # unaffected check reused


@pytest.mark.parametrize("sid", IDS)
def test_kill_switch_never_approves(sid):
    settings_store.set_("llm_enabled", "false")
    r = asyncio.run(scenarios.run(sid))
    assert r.status in ("pending", "rejected"), r.line()
    if r.status == "pending":
        assert r.route == "manual_review"
    # deterministic checks still ran and are visible
    assert db.query_one("SELECT COUNT(*) c FROM check_results WHERE run_id = ? AND check_name = 'bank_format'", (r.run_id,))["c"] == 1


SUSPICIOUS = {"S3", "S5a", "S5b", "S5c", "S6a", "S6b", "S6c", "S6d", "S7b"}


@pytest.mark.parametrize("sid", sorted(SUSPICIOUS & set(IDS)))
def test_inv3_suspicious_cases_get_no_vendor_text(sid):
    r = asyncio.run(scenarios.run(sid))
    form, _, _ = scenarios.load(sid)
    emails = db.query("SELECT * FROM outbox_emails WHERE run_id = ? AND audience = 'vendor'", (r.run_id,))  # the suspicious run
    assert emails
    for email in emails:
        assert email["kind"] in ("holding", "closure")
        for value in (form["legal_name"], form["contact_name"], form["tax_id"], form.get("bank_account_number") or form.get("iban")):
            assert value and value not in email["body"] and value not in email["subject"]


def test_inv3_vendor_request_contains_only_sanitised_names():
    r = asyncio.run(scenarios.run("S1"))
    email = db.query_one("SELECT * FROM outbox_emails WHERE submission_id = ? AND kind = 'request'", (r.submission_id,))
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789 .,&'-")
    assert "Meera Kulkarni" in email["body"] and "Marigold Packaging Private Limited" in email["body"]
    form, _, _ = scenarios.load("S1")
    assert form["tax_id"] not in email["body"] and form["bank_account_number"] not in email["body"]
    assert set(form["contact_name"]) <= allowed


def test_inv4_untrusted_values_are_escaped_everywhere():
    payload = '<script>alert("x")</script><img src=x onerror=alert(1)>'
    form, files, _ = scenarios.load("S0")
    form = {k: (payload if k not in ("country", "entity_type", "vendor_category") else v) for k, v in form.items()}
    from app.pipeline import runner
    sub_id, ver_id = runner.create_submission(form, {k: (payload + ".pdf", v[1]) for k, v in files.items()})
    run_id = db.execute("INSERT INTO runs(submission_id, version_id, trigger, started_at) VALUES (?,?,'initial',datetime('now'))", (sub_id, ver_id))
    asyncio.run(runner.execute(run_id))
    with TestClient(app) as client:
        pages = [f"/runs/{run_id}", "/watchlist", "/rules", "/settings"]
        pages += [f"/emails/{e['id']}" for e in db.query("SELECT id FROM outbox_emails WHERE submission_id = ?", (sub_id,))]
        for path in pages:
            html = client.get(path).text
            assert "<script>alert" not in html and "<img src=x" not in html, path
        # The event API returns JSON data; run.js renders it with textContent only, never innerHTML.
        assert client.get(f"/api/runs/{run_id}/events.json").headers["content-type"].startswith("application/json")
