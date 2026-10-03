"""P1 infrastructure: seed, settings, clock, audit, events, auth, health."""
import asyncio
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import audit, clock, db, events, settings_store
from app.main import app
from app.seed import seed


def _make_run() -> int:
    now = clock.now_iso()
    sid = db.execute("INSERT INTO submissions(created_at, updated_at) VALUES (?, ?)", (now, now))
    vid = db.execute("INSERT INTO submission_versions(submission_id, version_no, form_json, created_at) VALUES (?, 1, '{}', ?)", (sid, now))
    return db.execute("INSERT INTO runs(submission_id, version_id, trigger, started_at) VALUES (?, ?, 'initial', ?)", (sid, vid, now))


def test_seed_is_idempotent():
    counts = {t: db.query_one(f"SELECT COUNT(*) c FROM {t}")["c"] for t in ("vendors", "sanctions_list", "watchlist", "tax_rules")}
    assert counts == {"vendors": 12, "sanctions_list": 8, "watchlist": 6, "tax_rules": 5}
    assert seed() == {}
    again = {t: db.query_one(f"SELECT COUNT(*) c FROM {t}")["c"] for t in counts}
    assert again == counts


def test_us_is_not_seeded_so_it_is_first_seen():
    assert db.query_one("SELECT 1 FROM tax_rules WHERE country = 'US'") is None


def test_settings_roundtrip_and_unknown_key():
    assert settings_store.get_bool("llm_enabled") is True
    settings_store.set_("llm_enabled", "false")
    assert settings_store.get_bool("llm_enabled") is False
    with pytest.raises(KeyError):
        settings_store.set_("not_a_setting", "x")


def test_clock_advance_and_reset():
    base = clock.today()
    clock.advance(7)
    assert (clock.today() - base).days == 7
    clock.reset()
    assert clock.today() == base


def test_audit_log_is_append_only():
    audit.log("system", "test", "x", 1, {"a": 1})
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE audit_log SET action = 'tampered'")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("DELETE FROM audit_log")


def test_events_persist_with_sequence_numbers():
    run_id = _make_run()
    events.emit(run_id, "intake", "stage_started", {})
    events.emit(run_id, "intake", "check_result", {"check": "file_type", "result": "pass"})
    hist = events.history(run_id)
    assert [e.seq for e in hist] == [1, 2]
    assert hist[1].payload["check"] == "file_type"
    assert [e.seq for e in events.history(run_id, after_seq=1)] == [2]


def test_live_subscriber_receives_emitted_events():
    run_id = _make_run()

    async def scenario():
        queue = events.subscribe(run_id)
        try:
            events.emit(run_id, "intake", "stage_started", {})
            ev = await asyncio.wait_for(queue.get(), timeout=2)
            return ev
        finally:
            events.unsubscribe(run_id, queue)

    ev = asyncio.run(scenario())
    assert ev.kind == "stage_started" and ev.seq == 1


def test_sse_replays_finished_run_from_db():
    run_id = _make_run()
    events.emit(run_id, "intake", "stage_started", {})
    events.emit(run_id, "intake", "stage_finished", {"duration_ms": 3})
    events.emit(run_id, "finalise", "run_finished", {"status": "approved"})
    db.execute("UPDATE runs SET finished_at = ? WHERE id = ?", (clock.now_iso(), run_id))
    with TestClient(app) as client:
        with client.stream("GET", f"/api/runs/{run_id}/events") as resp:
            body = "".join(resp.iter_text())
        assert resp.headers["content-type"].startswith("text/event-stream")
        assert body.count("id: ") == 3 and "event: end" in body
        # Reconnect with Last-Event-ID only sends the remainder.
        with client.stream("GET", f"/api/runs/{run_id}/events", headers={"Last-Event-ID": "2"}) as resp:
            body = "".join(resp.iter_text())
        assert body.count("id: ") == 1
        poll = client.get(f"/api/runs/{run_id}/events.json?after=1").json()
        assert len(poll["events"]) == 2 and poll["finished"] is True


def test_healthz_and_pages_render():
    with TestClient(app) as client:
        assert client.get("/healthz").json() == {"status": "ok"}
        for path in ("/submit", "/dashboard", "/review", "/awaiting", "/rules", "/watchlist", "/settings"):
            r = client.get(path)
            assert r.status_code == 200, path
        assert "Quickfix Industrial Supplies" in client.get("/watchlist").text


def test_auth_gate():
    from app.config import config
    object.__setattr__(config, "demo_password", "s3cret")  # frozen dataclass
    try:
        _check_auth_flow()
    finally:
        object.__setattr__(config, "demo_password", "")


def _check_auth_flow():
    with TestClient(app) as client:
        r = client.get("/dashboard", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/login")
        assert client.get("/api/runs/1/events.json").status_code == 401
        assert client.get("/healthz").status_code == 200
        assert client.post("/login", data={"password": "wrong", "next": "/dashboard"}).status_code == 401
        r = client.post("/login", data={"password": "s3cret", "next": "/dashboard"}, follow_redirects=False)
        assert r.status_code == 303
        assert client.get("/dashboard").status_code == 200


def test_settings_post_writes_audit():
    with TestClient(app) as client:
        client.post("/settings", data={"llm_enabled": "false", "llm_mode": "replay", "search_provider": "fixture", "reviewer_name": "Asha", "evaluator_view": "false"})
        assert settings_store.get("reviewer_name") == "Asha"
        row = db.query_one("SELECT * FROM audit_log WHERE action = 'settings_changed'")
        assert row is not None and row["actor"] == "reviewer:Demo Reviewer"
        client.post("/settings/reset")
        assert settings_store.get("reviewer_name") == "Demo Reviewer"
