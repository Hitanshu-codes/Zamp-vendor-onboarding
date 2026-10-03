"""Sanctions, watchlist and vendor-master checks against the seeded data."""
from app import clock
from app.domain import sanctions, vendor_master, watchlist
from app.domain.taxids import make_gstin


def test_sanctions_exact_rejects():
    hits = sanctions.screen([("legal name", "Crimson Delta Trading F.Z.E.")], sanctions.load_entries(), 88)
    out = sanctions.sanctions_outcome(hits)
    assert out.reason_code == "SANCTIONS_EXACT" and out.severity == "reject"


def test_sanctions_alias_exact():
    hits = sanctions.screen([("director", "I. D. Petrenko")], sanctions.load_entries(), 88)
    assert sanctions.sanctions_outcome(hits).reason_code == "SANCTIONS_EXACT"


def test_sanctions_fuzzy_escalates_never_rejects():
    hits = sanctions.screen([("legal name", "Pale Harbour Logistics Ltd")], sanctions.load_entries(), 88)
    out = sanctions.sanctions_outcome(hits)
    assert out.reason_code == "SANCTIONS_FUZZY" and out.severity == "escalate"


def test_sanctions_clean_name():
    hits = sanctions.screen([("legal name", "Northwind Logistics Private Limited")], sanctions.load_entries(), 88)
    assert sanctions.sanctions_outcome(hits).result == "pass"


def _wl(candidate, name="Fresh New Name Private Limited"):
    hits = watchlist.screen(candidate, watchlist.load_entries(clock.today()), 88)
    return watchlist.watchlist_outcome(hits, name)


def test_watchlist_linked_bank_account_rejects_and_explains():
    out = _wl({"name": ["Fresh New Name Private Limited"], "bank_account": ["9180 2004 5671 234"]})
    assert out.reason_code == "WATCHLIST_BLOCKED"
    assert "is new, but the bank account matches blocked watchlist entry" in out.explanation


def test_watchlist_high_email_domain_escalates():
    out = _wl({"email_domain": ["accounts@sunmark-exports.example"]})
    assert out.reason_code == "WATCHLIST_HIGH" and out.severity == "escalate"


def test_watchlist_fuzzy_blocked_name_only_escalates():
    out = _wl({"name": ["Quickfix Industrial Supply Private Limited"]})
    assert out.reason_code == "WATCHLIST_HIGH"


def test_watchlist_watch_is_soft():
    out = _wl({"email_domain": ["lotusbay.example"]})
    assert out.result == "warn" and out.reason_code == "WATCHLIST_WATCH" and out.risk_points == 2


def test_watchlist_clean():
    assert _wl({"name": ["Totally Unrelated Co"], "bank_account": ["111122223333"]}).result == "pass"


def test_duplicate_vendor_and_bank_collision():
    vendors = vendor_master.load_vendors()
    dup = vendor_master.duplicate_outcome(make_gstin("27", "AABCN4521K"), vendors)
    assert dup.reason_code == "DUPLICATE_VENDOR" and dup.severity == "reject"
    col = vendor_master.collision_outcome("5020 0012 345678", "Westgate Freight Private Limited", vendors)
    assert col.reason_code == "BANK_COLLISION" and "Northwind" in col.explanation
    same = vendor_master.collision_outcome("50200012345678", "Northwind Logistics Pvt. Ltd.", vendors)
    assert same.result == "pass"
    assert vendor_master.collision_outcome("99999999999", "Anyone", vendors).result == "pass"
