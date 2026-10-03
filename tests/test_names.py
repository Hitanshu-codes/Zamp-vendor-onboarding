from app.domain.names import band, is_mixed_script, match_names, name_outcome, normalize_name


def test_normalisation_canonicalises_suffixes_and_punctuation():
    assert normalize_name("Acme Pvt. Ltd.") == normalize_name("ACME Private Limited") == "acme private limited"
    assert normalize_name("Harbor & Main Co.") == "harbor and main company"
    assert normalize_name("Müller Gëräte GmbH") == "muller gerate gmbh"


def test_only_identical_names_pass_on_code():
    assert match_names("Acme Pvt. Ltd.", "Acme Private Limited").exact


def test_industries_vs_industrial_never_passes():
    m = match_names("Acme Industries", "Acme Industrial")
    assert not m.exact
    assert band(m, 80) == "ambiguous"
    # Even if an LLM says "same", meaningful word differences escalate.
    out = name_outcome(m, 80, "yes", True, "x")
    assert out.result == "fail" and out.reason_code == "NAME_LLM_DISAGREE"


def test_clear_mismatch_is_fixable():
    m = match_names("Northwind Logistics Private Limited", "Saffron Textiles Private Limited")
    out = name_outcome(m, 80, None, True, "x")
    assert out.reason_code == "NAME_MISMATCH_CLEAR" and out.severity == "fixable"


def test_legal_form_difference_passes_only_with_llm_agreement():
    m = match_names("Acme Ltd", "Acme Private Limited")
    assert m.only_benign_differences
    assert name_outcome(m, 80, "yes", True, "x").result == "pass"
    assert name_outcome(m, 80, None, False, "x").reason_code == "NAME_AMBIGUOUS"
    assert name_outcome(m, 80, "unsure", True, "x").reason_code == "NAME_AMBIGUOUS"


def test_transliteration_variant_is_benign():
    m = match_names("Mohammed Traders", "Muhammad Traders")
    assert m.only_benign_differences
    assert name_outcome(m, 80, "yes", True, "x").result == "pass"


def test_trade_name_pattern_goes_to_ambiguous_band():
    m = match_names("Bluepeak Software Solutions LLP t/a Bluepeak", "Bluepeak Software Solutions LLP")
    assert band(m, 80) == "ambiguous"


def test_mixed_script_homoglyph_escalates():
    homoglyph = "Northwind Logistics Privаte Limited"  # Cyrillic 'а'
    assert is_mixed_script(homoglyph)
    out = name_outcome(match_names(homoglyph, "Northwind Logistics Private Limited"), 80, "yes", True, "x")
    assert out.reason_code == "NAME_AMBIGUOUS" and "mixed" in out.evidence["note"]
