import pytest

from app.domain.account_holder import apply_llm, assess_holder, holder_outcome

LEGAL = "Northwind Logistics Private Limited"
DIRECTORS = ["Rajesh Kumar", "Anita Desai"]

# (holder, account_type, expected_label)
CASES = [
    ("Northwind Logistics Private Limited", "current", "company"),
    ("Northwind Logistics Pvt. Ltd.", "unknown", "company"),
    ("NORTHWIND LOGISTICS PVT LTD", "savings", "company"),
    ("Acme Widgets LLP", "unknown", "company"),
    ("Brightline Inc", "unknown", "company"),
    ("Harbor & Main Consulting GmbH", "current", "company"),
    ("Kaveri Agro Industries", "current", "company"),
    ("Saffron Traders", "current", "company"),
    ("Rajesh Kumar", "savings", "individual"),
    ("Rajesh Kumar", "unknown", "individual"),
    ("Mr. Rajesh Kumar", "current", "individual"),
    ("Smt. Lakshmi Iyer", "unknown", "individual"),
    ("Shri Ramesh Gupta", "unknown", "individual"),
    ("Dr Anita Desai", "unknown", "individual"),
    ("Ramesh S/O Suresh Gupta", "unknown", "individual"),
    ("Anita Desai and Rajesh Kumar", "unknown", "individual"),
    ("A. Desai / R. Kumar", "unknown", "individual"),
    ("Priya Nair", "savings", "individual"),
    ("Sharma Brothers", "unknown", "unclear"),
    ("Raj Kumar Traders", "unknown", "unclear"),
    ("Mr Rajesh Kumar Pvt Ltd", "unknown", "unclear"),
    ("Delta", "unknown", "unclear"),
]


@pytest.mark.parametrize("holder,account_type,label", CASES)
def test_holder_label_table(holder, account_type, label):
    assert assess_holder(holder, LEGAL, DIRECTORS, account_type).label == label


def test_table_has_at_least_20_cases():
    assert len(CASES) >= 20


def test_director_savings_account_for_company_is_fixable():
    a = assess_holder("Rajesh Kumar", LEGAL, DIRECTORS, "savings")
    assert a.matches_director == "Rajesh Kumar"
    out = holder_outcome(a, "company", [], [])
    assert out.reason_code == "BANK_HOLDER_NOT_COMPANY" and out.severity == "fixable"
    assert "matches director" in out.explanation


def test_individual_plus_other_anomaly_escalates():
    a = assess_holder("Rajesh Kumar", LEGAL, DIRECTORS, "savings")
    assert holder_outcome(a, "company", [], ["bank_collision"]).reason_code == "ENTITY_TYPE_UNCLEAR"


def test_sole_proprietor_with_own_account_passes():
    a = assess_holder("Priya Nair", "Priya Nair", ["Priya Nair"], "savings")
    assert holder_outcome(a, "sole_proprietor", ["Priya Nair"], []).result == "pass"


def test_company_account_passes():
    a = assess_holder(LEGAL, LEGAL, DIRECTORS, "current")
    assert holder_outcome(a, "company", [], []).result == "pass"


def test_llm_can_only_resolve_unclear():
    unclear = assess_holder("Sharma Brothers", LEGAL, DIRECTORS, "unknown")
    assert apply_llm(unclear, "company").label == "company"
    individual = assess_holder("Rajesh Kumar", LEGAL, DIRECTORS, "savings")
    assert apply_llm(individual, "company").label == "individual"  # cannot override code
    unclear2 = assess_holder("Delta", LEGAL, DIRECTORS, "unknown")
    assert apply_llm(unclear2, "garbage").label == "unclear"


def test_unclear_escalates():
    a = assess_holder("Sharma Brothers", LEGAL, DIRECTORS, "unknown")
    assert holder_outcome(a, "company", [], []).reason_code == "ENTITY_TYPE_UNCLEAR"
