"""Dashboard KPIs/filters/drawer/export and watchlist administration."""
import asyncio

from fastapi.testclient import TestClient

from app import clock, db, scenarios, watchlist_admin
from app.domain import watchlist
from app.main import app


def run(coro):
    return asyncio.run(coro)


def test_dashboard_kpis_filters_and_charts():
    for sid in ("S0", "S1", "S3", "S6a"):
        run(scenarios.run(sid))
    with TestClient(app) as client:
        html = client.get("/dashboard").text
        assert "Auto-decided" in html and "75%" in html  # S0, S1, S6a decided by the engine; S3 needs a person
        assert "Top reason codes" in html and "BANK_COLLISION" in html and "Outcomes by submission day" in html
        only_review = client.get("/dashboard?status=manual_review").text
        assert "Westgate Freight" in only_review and "Evergreen Fabrication" not in only_review
        by_code = client.get("/dashboard?code=DOC_WRONG_TYPE").text
        assert "Marigold Packaging" in by_code and "Westgate Freight" not in by_code
        assert "No submissions match" in client.get("/dashboard?q=nonexistent").text


def test_drawer_tabs_versions_and_audit():
    r = run(scenarios.run("S7a"))
    with TestClient(app) as client:
        html = client.get(f"/submissions/{r.submission_id}/drawer").text
        for tab in ("Reasoning", "Fields", "Documents", "Emails", "Versions (2)", "Audit trail"):
            assert tab in html
        assert "tax cert (document)" in html  # version diff shows the replaced document
        assert "version created" in html and "run finished" in html  # audit timeline
        assert "llm call:classify extract" in html and "decision made" in html and "email prepared" in html
        assert client.get(f"/submissions/{r.submission_id}").status_code == 200


def test_export_json_contains_everything():
    r = run(scenarios.run("S3"))
    with TestClient(app) as client:
        data = client.get(f"/submissions/{r.submission_id}/export.json").json()
    assert data["submission"]["route"] == "manual_review"
    assert any(c["check_name"] == "bank_collision" and c["result"] == "fail" for c in data["latest_checks"])
    assert data["decisions"][0]["reason_codes"][0]["code"] == "BANK_COLLISION"
    assert data["audit_trail"] and data["emails"] and data["review_cases"]
    assert "1" in data["documents"] and data["documents"]["1"]["tax_cert"]["extraction"]["doc_type"] == "tax_registration_certificate"


def test_drawer_escapes_untrusted_values():
    form, files, _ = scenarios.load("S0")
    form["legal_name"] = '<script>alert(1)</script> Evil Pvt Ltd'
    from app.pipeline import runner
    sub_id, ver_id = runner.create_submission(form, files)
    run_id = db.execute("INSERT INTO runs(submission_id, version_id, trigger, started_at) VALUES (?,?,'initial',?)", (sub_id, ver_id, clock.now_iso()))
    run(runner.execute(run_id))
    with TestClient(app) as client:
        for path in (f"/submissions/{sub_id}/drawer", f"/submissions/{sub_id}", "/dashboard"):
            assert "<script>alert(1)" not in client.get(path).text, path


def test_watchlist_add_deactivate_and_screening():
    wid = watchlist_admin.add_entry("Test entry", "high", "Testing manual entry", [("bank_account", "1234 5678 9012 34"), ("phone", "")], None, "reviewer:t")
    entries = watchlist.load_entries(clock.today())
    hits = watchlist.screen({"bank_account": ["12345678901234"]}, entries, 88)
    assert any(h.entry.id == wid for h in hits)
    watchlist_admin.set_active(wid, False, "False positive, closed", "reviewer:t")
    hits = watchlist.screen({"bank_account": ["12345678901234"]}, watchlist.load_entries(clock.today()), 88)
    assert not any(h.entry.id == wid for h in hits)
    assert db.query_one("SELECT 1 FROM audit_log WHERE action = 'watchlist_deactivated'")


def test_expired_entries_are_not_screened():
    wid = watchlist_admin.add_entry("Expired", "blocked", "Old case, expired", [("tax_id", "EXPIRED123")], "2020-01-01", "reviewer:t")
    assert all(e.id != wid for e in watchlist.load_entries(clock.today()))


def test_csv_import_groups_rows_and_reports_errors():
    csv_text = ("label,severity,reason,kind,value\n"
                "Acme Shell,blocked,Shell company,name,Acme Shell Traders\n"
                "Acme Shell,blocked,Shell company,bank_account,999988887777\n"
                "Lone Watch,watch,Late deliveries,email_domain,lone.example\n"
                "Bad Row,high,whatever,colour,blue\n")
    created, errors = watchlist_admin.import_csv(csv_text, "reviewer:t")
    assert created == 2 and len(errors) == 1 and "unknown kind" in errors[0]
    acme = db.query_one("SELECT id FROM watchlist WHERE label = 'Acme Shell'")
    assert db.query_one("SELECT COUNT(*) c FROM watchlist_identifiers WHERE watchlist_id = ?", (acme["id"],))["c"] == 2


def test_watchlist_page_actions_over_http():
    with TestClient(app) as client:
        r = client.post("/watchlist/add", data={"label": "Via form", "severity": "watch", "reason": "Added from the page",
                                                "kind": ["name", "bank_account"], "value": ["Via Form Co", ""]}, follow_redirects=False)
        assert r.status_code == 303 and "done=" in r.headers["location"]
        bad = client.post("/watchlist/add", data={"label": "x", "severity": "watch", "reason": "no identifiers here", "kind": ["name"], "value": [""]},
                          follow_redirects=False)
        assert "error=" in bad.headers["location"]
        page = client.get("/watchlist").text
        assert "Via form" in page and "Import from CSV" in page
