"""SQLite access: schema, connections, small helpers. Plain sqlite3, WAL mode."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Iterator

from app.config import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS vendors (
    id INTEGER PRIMARY KEY,
    legal_name TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    country TEXT NOT NULL,
    tax_id TEXT,
    bank_account TEXT,
    bank_code TEXT,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','blocked')),
    email_domain TEXT,
    contact_phone TEXT,
    payment_hold INTEGER NOT NULL DEFAULT 0,
    source_submission_id INTEGER,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_vendors_tax ON vendors(tax_id);
CREATE INDEX IF NOT EXISTS ix_vendors_bank ON vendors(bank_account);

CREATE TABLE IF NOT EXISTS submissions (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    current_version INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('approved','pending','rejected')),
    route TEXT NOT NULL DEFAULT 'none' CHECK (route IN ('none','vendor_followup','manual_review')),
    vendor_ref TEXT,
    vendor_name TEXT,
    country TEXT,
    risk_points INTEGER NOT NULL DEFAULT 0,
    top_reason_codes_json TEXT NOT NULL DEFAULT '[]',
    closed_reason TEXT,
    resubmit_token TEXT UNIQUE,
    requested_items_json TEXT NOT NULL DEFAULT '[]',
    due_at TEXT,
    reminders_sent INTEGER NOT NULL DEFAULT 0,
    vendor_id INTEGER REFERENCES vendors(id)
);
CREATE INDEX IF NOT EXISTS ix_submissions_status ON submissions(status, route);

CREATE TABLE IF NOT EXISTS submission_versions (
    id INTEGER PRIMARY KEY,
    submission_id INTEGER NOT NULL REFERENCES submissions(id),
    version_no INTEGER NOT NULL,
    form_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    diff_from_previous_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE (submission_id, version_no)
);

CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY,
    version_id INTEGER NOT NULL REFERENCES submission_versions(id),
    slot TEXT NOT NULL CHECK (slot IN ('tax_cert','bank_letter','incorporation_cert')),
    filename TEXT NOT NULL,
    storage_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    mime TEXT,
    size INTEGER,
    page_count INTEGER,
    classified_type TEXT,
    extraction_json TEXT,
    extraction_quality TEXT,
    hidden_text_findings_json TEXT
);
CREATE INDEX IF NOT EXISTS ix_documents_version ON documents(version_id);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    submission_id INTEGER NOT NULL REFERENCES submissions(id),
    version_id INTEGER NOT NULL REFERENCES submission_versions(id),
    trigger TEXT NOT NULL CHECK (trigger IN ('initial','resubmission','bank_change','manual_rerun')),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    outcome_status TEXT,
    outcome_route TEXT
);
CREATE INDEX IF NOT EXISTS ix_runs_submission ON runs(submission_id);

CREATE TABLE IF NOT EXISTS run_events (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    seq INTEGER NOT NULL,
    ts TEXT NOT NULL,
    stage TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('stage_started','check_result','stage_finished','info','error','run_finished')),
    payload_json TEXT NOT NULL,
    UNIQUE (run_id, seq)
);

CREATE TABLE IF NOT EXISTS check_results (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    stage TEXT NOT NULL,
    check_name TEXT NOT NULL,
    result TEXT NOT NULL CHECK (result IN ('pass','fail','warn','skipped')),
    severity TEXT NOT NULL CHECK (severity IN ('info','soft','fixable','escalate','reject')),
    reason_code TEXT,
    risk_points INTEGER NOT NULL DEFAULT 0,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    explanation TEXT NOT NULL,
    inputs_hash TEXT,
    blocked_by TEXT,
    required INTEGER NOT NULL DEFAULT 0,
    reused_from_run_id INTEGER
);
CREATE INDEX IF NOT EXISTS ix_checks_run ON check_results(run_id);

CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY,
    run_id INTEGER REFERENCES runs(id),
    submission_id INTEGER NOT NULL REFERENCES submissions(id),
    ts TEXT NOT NULL,
    status TEXT NOT NULL,
    route TEXT NOT NULL,
    reason_codes_json TEXT NOT NULL,
    risk_points INTEGER NOT NULL DEFAULT 0,
    rationale_text TEXT NOT NULL,
    decided_by TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS outbox_emails (
    id INTEGER PRIMARY KEY,
    submission_id INTEGER NOT NULL REFERENCES submissions(id),
    run_id INTEGER,
    kind TEXT NOT NULL,
    audience TEXT NOT NULL CHECK (audience IN ('vendor','internal')),
    "to" TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    reason_codes_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','queued_demo_sent','sent','failed')),
    provider TEXT,
    provider_message_id TEXT,
    delivered_to TEXT,
    error TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_outbox_submission ON outbox_emails(submission_id);

CREATE TABLE IF NOT EXISTS review_cases (
    id INTEGER PRIMARY KEY,
    submission_id INTEGER NOT NULL REFERENCES submissions(id),
    run_id INTEGER,
    opened_at TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 3,
    reasons_json TEXT NOT NULL DEFAULT '[]',
    tasks_json TEXT NOT NULL DEFAULT '[]',
    assignee TEXT,
    state TEXT NOT NULL DEFAULT 'open' CHECK (state IN ('open','resolved'))
);
CREATE INDEX IF NOT EXISTS ix_cases_state ON review_cases(state, priority);

CREATE TABLE IF NOT EXISTS review_actions (
    id INTEGER PRIMARY KEY,
    case_id INTEGER NOT NULL REFERENCES review_cases(id),
    ts TEXT NOT NULL,
    reviewer TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('approve','reject','request_info','clear_hit','add_watchlist','verify_rule','disable_rule','edit_rule')),
    reason_text TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS sanctions_list (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    aliases_json TEXT NOT NULL DEFAULT '[]',
    country TEXT,
    entity_type TEXT,
    source_label TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS watchlist (
    id INTEGER PRIMARY KEY,
    label TEXT NOT NULL,
    severity TEXT NOT NULL CHECK (severity IN ('blocked','high','watch')),
    reason TEXT NOT NULL,
    source_submission_id INTEGER,
    added_by TEXT NOT NULL,
    added_at TEXT NOT NULL,
    expires_at TEXT,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS watchlist_identifiers (
    id INTEGER PRIMARY KEY,
    watchlist_id INTEGER NOT NULL REFERENCES watchlist(id),
    kind TEXT NOT NULL CHECK (kind IN ('name','bank_account','tax_id','email_domain','phone','address','director')),
    value_normalized TEXT NOT NULL,
    value_display TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_wl_ident ON watchlist_identifiers(kind, value_normalized);

CREATE TABLE IF NOT EXISTS watchlist_matches (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    watchlist_id INTEGER NOT NULL REFERENCES watchlist(id),
    matched_kind TEXT NOT NULL,
    match_type TEXT NOT NULL CHECK (match_type IN ('exact','fuzzy')),
    score REAL,
    cleared_by TEXT,
    cleared_reason TEXT
);

CREATE TABLE IF NOT EXISTS tax_rules (
    id INTEGER PRIMARY KEY,
    country TEXT NOT NULL,
    tax_type TEXT NOT NULL,
    description TEXT NOT NULL,
    regex TEXT NOT NULL,
    checksum_algo TEXT,
    example TEXT,
    source_url TEXT,
    status TEXT NOT NULL CHECK (status IN ('seed','verified','provisional','disabled')),
    times_used INTEGER NOT NULL DEFAULT 0,
    times_confirmed INTEGER NOT NULL DEFAULT 0,
    confirmed_vendor_refs_json TEXT NOT NULL DEFAULT '[]',
    last_verified_at TEXT,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL CHECK (created_by IN ('seed','search','reviewer')),
    notes TEXT
);
CREATE INDEX IF NOT EXISTS ix_rules_key ON tax_rules(country, tax_type);

CREATE TABLE IF NOT EXISTS tax_rule_search_log (
    id INTEGER PRIMARY KEY,
    country TEXT NOT NULL,
    tax_type TEXT NOT NULL,
    query TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('found','not_found','failed_selftest','error','rejected_injection')),
    ts TEXT NOT NULL,
    source_urls_json TEXT NOT NULL DEFAULT '[]',
    notes TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    entity_type TEXT,
    entity_id TEXT,
    detail_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_audit_entity ON audit_log(entity_type, entity_id);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS llm_usage (
    day TEXT PRIMARY KEY,
    calls INTEGER NOT NULL DEFAULT 0
);

-- Append-only audit log: the database refuses updates and deletes.
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
"""

# Tables wiped by "Reset demo data" (order respects foreign keys). audit_log is
# append-only, so a reset drops and recreates the whole file instead.
_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    config.data_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.db_path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """One short-lived connection per unit of work; commits on success."""
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with _lock:
        conn = _connect()
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(SCHEMA)
            conn.commit()
        finally:
            conn.close()


def drop_db() -> None:
    """Delete the database file (used by Reset demo data and tests)."""
    with _lock:
        for suffix in ("", "-wal", "-shm"):
            p = config.db_path.with_name(config.db_path.name + suffix)
            if p.exists():
                p.unlink()


def query(sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(sql, params).fetchall()


def query_one(sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
    with connect() as conn:
        return conn.execute(sql, params).fetchone()


def execute(sql: str, params: tuple | dict = ()) -> int:
    """Run a write and return lastrowid."""
    with connect() as conn:
        cur = conn.execute(sql, params)
        return cur.lastrowid


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str, sort_keys=True)


def loads(s: str | None, default: Any = None) -> Any:
    if not s:
        return default
    return json.loads(s)
