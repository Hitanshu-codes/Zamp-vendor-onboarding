"""Tax-rule learning: allowlist, poisoned sources, self-test, provisional storage, cache hit, negative cache, promotion."""
import asyncio

from fastapi.testclient import TestClient

from app import clock, db, scenarios, search, settings_store, tax_registry
from app.main import app
from app.pipeline.tax_learning import learn, recent_miss, template_examples


class FakeCtx:
    def __init__(self):
        self.messages, self.llm_failures = [], []

    def info(self, message, **data):
        self.messages.append(message)


def run(coro):
    return asyncio.run(coro)


def test_allowlist_checks_hostnames_not_substrings():
    assert search.host_allowed("https://www.irs.gov/pub/x.pdf")
    assert search.host_allowed("https://sub.gov.uk/page")
    assert not search.host_allowed("https://irs.gov.evil.example/page")
    assert not search.host_allowed("https://notirs.gov.example.com")
    assert not search.host_allowed("https://rut-checker.example.com/chile")


def test_template_examples():
    assert template_examples("XX-XXXXXXX") == "47-2918365"
    assert template_examples("AAAAA9999A") == "KQRTV4729W"


def test_focus_keeps_the_relevant_part_of_long_pages():
    text = ("filler " * 5000) + "The EIN format is XX-XXXXXXX." + (" more" * 5000)
    focused = search.focus(text, ["EIN"])
    assert "XX-XXXXXXX" in focused and len(focused) <= search.MAX_PAGE_CHARS


def test_html_to_text_strips_scripts():
    assert search.html_to_text("<html><script>evil()</script><p>EIN is <b>nine</b> digits</p></html>") == "EIN is nine digits"


def test_poisoned_fixture_never_yields_a_rule_and_is_negatively_cached():
    settings_store.set_("search_provider", "fixture")
    settings_store.set_("llm_mode", "replay")
    ctx = FakeCtx()
    result = run(learn(ctx, "CL", "RUT"))
    assert result.rule is None
    outcomes = [s.get("outcome", "") for s in result.evidence["sources"]]
    assert any(o.startswith("skipped: not on the allowlist") for o in outcomes)
    assert any(o.startswith("rejected: page contains instruction-like text") for o in outcomes)
    assert any("failed self-test" in o for o in outcomes)
    assert tax_registry.find_rule("CL", "RUT") is None
    assert recent_miss("CL", "RUT")
    again = run(learn(FakeCtx(), "CL", "RUT"))
    assert again.evidence.get("negative_cache") is True


def test_first_seen_country_learns_provisional_then_cache_hit():
    r = run(scenarios.run("S4a"))
    assert r.ok, r.line()
    rule = tax_registry.find_rule("US", "EIN")
    assert rule is not None and rule.status == "provisional" and "irs.gov" in rule.source_url
    r2 = run(scenarios.run("S4b"))
    ev = db.loads(db.query_one("SELECT evidence_json FROM check_results WHERE run_id = ? AND check_name = 'tax_rule'", (r2.run_id,))["evidence_json"])
    assert ev["cache"] == "hit"


def test_bad_id_against_provisional_rule_escalates():
    run(scenarios.run("S4a"))
    r = run(scenarios.run("S4c"))
    assert r.ok and r.codes == ["RULE_PROVISIONAL_MISMATCH"]


def test_provisional_rule_promoted_after_n_approvals(monkeypatch):
    run(scenarios.run("S4a"))
    run(scenarios.run("S4b"))
    rule = db.query_one("SELECT * FROM tax_rules WHERE country = 'US'")
    assert rule["times_confirmed"] == 2 and rule["status"] == "provisional"
    # A third distinct approved vendor using the rule promotes it.
    sub = db.query_one("SELECT id FROM submissions ORDER BY id DESC LIMIT 1")["id"]
    run_id = db.query_one("SELECT id FROM runs WHERE submission_id = ?", (sub,))["id"]
    new_sub = db.execute("INSERT INTO submissions(created_at, updated_at, vendor_name) VALUES (?,?, 'Third Vendor Inc')", (clock.now_iso(), clock.now_iso()))
    ver = db.execute("INSERT INTO submission_versions(submission_id, version_no, form_json, created_at) VALUES (?,1,'{}',?)", (new_sub, clock.now_iso()))
    new_run = db.execute("INSERT INTO runs(submission_id, version_id, trigger, started_at) VALUES (?,?,'initial',?)", (new_sub, ver, clock.now_iso()))
    ev = db.query_one("SELECT evidence_json FROM check_results WHERE run_id = ? AND check_name = 'tax_rule'", (run_id,))["evidence_json"]
    db.execute("INSERT INTO check_results(run_id, stage, check_name, result, severity, evidence_json, explanation) VALUES (?,?,?,?,?,?,?)",
               (new_run, "tax_rule_lookup", "tax_rule", "pass", "info", ev, "x"))
    tax_registry.confirm_on_approval(new_sub)
    assert db.query_one("SELECT status FROM tax_rules WHERE country = 'US'")["status"] == "verified"


def test_rule_admin_actions_are_audited_and_validated():
    run(scenarios.run("S4a"))
    rid = db.query_one("SELECT id FROM tax_rules WHERE country = 'US'")["id"]
    with TestClient(app) as client:
        r = client.post(f"/rules/{rid}/edit", data={"regex": ".*", "example": "47-2918365", "reason": "trying a bad edit", "description": ""})
        assert "failed the self-test" in client.get(r.headers.get("location", "/rules")).text or r.status_code in (200, 303)
        assert db.query_one("SELECT regex FROM tax_rules WHERE id = ?", (rid,))["regex"] != ".*"
        client.post(f"/rules/{rid}/verify", data={"reason": "Checked against IRS Publication 1635."})
        assert db.query_one("SELECT status FROM tax_rules WHERE id = ?", (rid,))["status"] == "verified"
        client.post(f"/rules/{rid}/disable", data={"reason": "Testing disable."})
        assert tax_registry.find_rule("US", "EIN") is None
        client.post(f"/rules/{rid}/forget", data={})
        assert db.query_one("SELECT 1 FROM tax_rules WHERE id = ?", (rid,)) is None
        actions = {row["action"] for row in db.query("SELECT action FROM audit_log WHERE entity_type = 'tax_rule'")}
        assert {"tax_rule_learned", "tax_rule_verified", "tax_rule_disabled", "tax_rule_forgotten"} <= actions
        assert client.get("/rules").status_code == 200


def test_seed_rules_cannot_be_forgotten():
    rid = db.query_one("SELECT id FROM tax_rules WHERE country = 'IN' AND tax_type = 'GSTIN'")["id"]
    try:
        tax_registry.forget(rid, "reviewer:x")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
