from datetime import date

import pytest

from app.domain import banking, validators
from app.domain.rule_selftest import self_test
from app.domain.taxids import (TaxRule, gstin_checksum_ok, gstin_pan_outcome, interpret, make_gstin, pan_entity_outcome,
                               safe_fullmatch)

GSTIN_RULE = TaxRule(1, "IN", "GSTIN", r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$", "seed", "gstin_mod36")
DE_RULE = TaxRule(2, "DE", "VAT", r"^DE[0-9]{9}$", "seed")
GB_RULE = TaxRule(3, "GB", "VAT", r"^GB([0-9]{9}|[0-9]{12}|GD[0-9]{3}|HA[0-9]{3})$", "seed")
US_PROVISIONAL = TaxRule(4, "US", "EIN", r"^[0-9]{2}-?[0-9]{7}$", "provisional")


# --- GSTIN / PAN ------------------------------------------------------------

@pytest.mark.parametrize("gstin", ["27AAPFU0939F1ZV", "29AAGCB7383J1Z4", "33GSPTN0231G1ZM"])
def test_publicly_documented_gstins_pass_checksum(gstin):
    assert gstin_checksum_ok(gstin)


@pytest.mark.parametrize("pan,state", [("AABCN4521K", "27"), ("AAQFB2209D", "29"), ("ABCPK1234L", "07")])
def test_generated_gstins_are_valid_and_embed_pan(pan, state):
    g = make_gstin(state, pan)
    assert gstin_checksum_ok(g) and g[2:12] == pan and GSTIN_RULE.matches(g)


def test_gstin_bad_values():
    assert not gstin_checksum_ok("27AAPFU0939F1ZX")
    assert not gstin_checksum_ok("27AAPFU0939F1Z")
    assert not gstin_checksum_ok("27aapfu0939f1zv")


def test_interpret_pass_and_checksum_typo():
    assert interpret("27AAPFU0939F1ZV", GSTIN_RULE, [DE_RULE]).result == "pass"
    out = interpret("27AAPFU0939F1ZX", GSTIN_RULE, [DE_RULE])
    assert out.reason_code == "TAXID_CHECKSUM_FAIL" and out.severity == "fixable"


def test_interpret_one_character_typo_in_format():
    out = interpret("27AAPFU0939FIZV", GSTIN_RULE, [])  # 'I' typed where the PAN-check letter must... still fails format
    assert out.reason_code in ("TAXID_CHECKSUM_FAIL", "TAXID_FORMAT_FAIL")
    out = interpret("2OAAPFU0939F1ZV", GSTIN_RULE, [])  # letter O instead of zero in the state code
    assert out.reason_code == "TAXID_CHECKSUM_FAIL" and out.evidence.get("one_character_from_valid")


def test_interpret_wrong_country_format_escalates():
    out = interpret("DE812345678", GSTIN_RULE, [DE_RULE, GB_RULE])
    assert out.reason_code == "TAXID_WRONG_COUNTRY_FORMAT" and out.severity == "escalate"


def test_interpret_format_fail_against_seed_rule():
    assert interpret("HELLO", GSTIN_RULE, [DE_RULE]).reason_code == "TAXID_FORMAT_FAIL"


def test_interpret_provisional_mismatch_never_rejects():
    out = interpret("12-34X6789", US_PROVISIONAL, [DE_RULE])
    assert out.reason_code == "RULE_PROVISIONAL_MISMATCH" and out.severity == "escalate"
    assert interpret("12-3456789", US_PROVISIONAL, []).result == "pass"


def test_pan_entity_code():
    assert pan_entity_outcome("AABCN4521K", "company").result == "pass"
    assert pan_entity_outcome("ABCPK1234L", "company").reason_code == "ENTITY_TYPE_UNCLEAR"
    assert pan_entity_outcome("ABCPK1234L", "sole_proprietor").result == "pass"


def test_gstin_pan_consistency():
    g = make_gstin("27", "AABCN4521K")
    assert gstin_pan_outcome(g, "AABCN4521K").result == "pass"
    assert gstin_pan_outcome(g, "AABCX9999K").reason_code == "TAXID_INCONSISTENT"


# --- regex safety and self-test --------------------------------------------

def test_regex_timeout_counts_as_no_match():
    assert safe_fullmatch(r"^(a+)+$", "a" * 40 + "b") is False


def test_selftest_accepts_a_sane_rule():
    r = self_test(r"^[0-9]{2}-?[0-9]{7}$", ["12-3456789", "98-7654321"])
    assert r.passed, r.reasons


@pytest.mark.parametrize("pattern,reason_part", [
    (r".*", "accepts anything"),
    (r"^(\d+)+$", "nested quantifiers"),
    (r"^[A-Z0-9 .\-/]+$", "random strings"),
    (r"^[0-9]{3}$", "matched only"),
    ("x" * 250, "longer than"),
])
def test_selftest_rejects_bad_rules(pattern, reason_part):
    r = self_test(pattern, ["12-3456789"])
    assert not r.passed and any(reason_part in x for x in r.reasons), r.reasons


def test_selftest_requires_examples():
    assert not self_test(r"^[0-9]{9}$", []).passed


# --- banking ---------------------------------------------------------------

@pytest.mark.parametrize("iban", ["GB82WEST12345698765432", "DE89370400440532013000", "FR1420041010050500013M02606", "GB33BUKB20201555555555"])
def test_valid_ibans(iban):
    assert banking.iban_valid(iban)[0]


@pytest.mark.parametrize("iban", ["GB82WEST12345698765433", "DE8937040044053201300", "ZZ00123456789012"])
def test_invalid_ibans(iban):
    assert not banking.iban_valid(iban)[0]


def test_aba_ifsc_swift():
    assert banking.aba_valid("021000021") and banking.aba_valid("011000015")
    assert not banking.aba_valid("021000022")
    assert banking.ifsc_valid("HDFC0001234") and not banking.ifsc_valid("HDFC1001234")
    assert banking.swift_valid("DEUTDEFF") and banking.swift_valid("DEUTDEFF500") and not banking.swift_valid("DEUT")


def test_validate_bank_india_and_typos():
    assert banking.validate_bank("IN", "50200012345678", "HDFC0001234").result == "pass"
    bad = banking.validate_bank("IN", "5020", "HDFC1001234")
    assert bad.reason_code == "BANK_DETAILS_INVALID" and len(bad.evidence["problems"]) == 2


def test_bank_country_mismatch():
    assert banking.bank_country_outcome("IN", "DE89370400440532013000", "COBADEFFXXX").reason_code == "BANK_COUNTRY_MISMATCH"
    assert banking.bank_country_outcome("DE", "DE89370400440532013000", "COBADEFFXXX").result == "pass"
    assert banking.bank_country_outcome("IN", "DE89370400440532013000", "COBADEFFXXX", exempt=True).result == "pass"


# --- dates / email -----------------------------------------------------------

TODAY = date(2026, 10, 3)


def test_expiry_and_staleness():
    assert validators.expiry_outcome("tax certificate", "2026-01-01", TODAY).reason_code == "DOC_EXPIRED"
    assert validators.expiry_outcome("tax certificate", "2027-01-01", TODAY).result == "pass"
    assert validators.bank_letter_age_outcome("2026-05-01", TODAY, 90).reason_code == "DOC_STALE"
    assert validators.bank_letter_age_outcome("2026-09-20", TODAY, 90).result == "pass"


def test_date_order():
    assert validators.date_order_outcome("2019-01-01", "2020-01-01").reason_code == "DATES_IMPOSSIBLE"
    assert validators.date_order_outcome("2021-01-01", "2020-01-01").result == "pass"
    assert validators.date_order_outcome(None, "2020-01-01").result == "skipped"


def test_free_email_is_soft():
    out = validators.email_outcome("owner@gmail.com", "company")
    assert out.result == "warn" and out.reason_code == "FREE_EMAIL_DOMAIN" and out.risk_points == 2
    assert validators.email_outcome("owner@gmail.com", "sole_proprietor").result == "pass"
