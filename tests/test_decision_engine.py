"""Decision engine: precedence table, missing-check rule, and invariants INV-1 / INV-2."""
from hypothesis import given, settings
from hypothesis import strategies as st

from app.decision import reason_codes
from app.decision.engine import decide
from app.models import CheckOutcome, CheckResult, DecisionFlags, failed, passed, skipped, warned

REQ = ["a", "b", "c"]


def cr(name: str, outcome: CheckOutcome, required: bool = True) -> CheckResult:
    return CheckResult.of(name, "test", outcome, required=required)


def flags(**kw) -> DecisionFlags:
    return DecisionFlags(required_checks=kw.pop("required", REQ), **kw)


ALL_PASS = [cr("a", passed("ok")), cr("b", passed("ok")), cr("c", passed("ok"))]


def test_all_pass_approves():
    d = decide(ALL_PASS, flags())
    assert (d.status, d.route, d.vendor_message) == ("approved", "none", "approval")


def test_fixable_goes_to_vendor():
    d = decide([cr("a", failed("DOC_WRONG_TYPE", "x")), *ALL_PASS[1:]], flags())
    assert (d.status, d.route, d.vendor_message) == ("pending", "vendor_followup", "request")


def test_escalate_beats_fixable():
    res = [cr("a", failed("DOC_WRONG_TYPE", "x")), cr("b", failed("BANK_COLLISION", "x")), ALL_PASS[2]]
    d = decide(res, flags())
    assert (d.route, d.vendor_message) == ("manual_review", "holding")
    assert {t.code for t in d.triggered} == {"DOC_WRONG_TYPE", "BANK_COLLISION"}  # reviewer still sees the fixable item


def test_reject_beats_everything():
    res = [cr("a", failed("SANCTIONS_EXACT", "x")), cr("b", failed("BANK_COLLISION", "x")), cr("c", failed("DOC_MISSING", "x"))]
    d = decide(res, flags())
    assert (d.status, d.route, d.vendor_message) == ("rejected", "none", "closure")
    assert d.triggered[0].code == "SANCTIONS_EXACT"


def test_missing_required_check_is_a_failure_not_a_pass():
    d = decide(ALL_PASS[:2], flags())
    assert d.status == "pending" and d.route == "manual_review" and d.missing_required == ["c"]
    assert any(t.code == "CHECK_MISSING" for t in d.triggered)


def test_skipped_required_check_blocks_approval():
    d = decide([*ALL_PASS[:2], cr("c", skipped("not run"))], flags())
    assert d.status != "approved"


def test_skip_blocked_by_a_real_failure_is_covered():
    res = [cr("a", failed("DOC_WRONG_TYPE", "x")), cr("b", skipped("needs a", blocked_by="a")), ALL_PASS[2]]
    d = decide(res, flags())
    assert d.route == "vendor_followup" and not any(t.code == "CHECK_MISSING" for t in d.triggered)


def test_skip_blocked_by_a_passing_check_is_not_covered():
    res = [cr("a", passed("ok")), cr("b", skipped("claims a", blocked_by="a")), ALL_PASS[2]]
    d = decide(res, flags())
    assert d.route == "manual_review" and any(t.code == "CHECK_MISSING" for t in d.triggered)


def test_soft_points_reach_threshold():
    res = [*ALL_PASS, cr("e", warned("x", code="FREE_EMAIL_DOMAIN"), False), cr("w", warned("x", code="WATCHLIST_WATCH"), False),
           cr("q", warned("x", code="DEGRADED_DOCUMENT"), False)]
    d = decide(res, flags(risk_threshold=5))
    assert d.risk_points == 5 and d.route == "manual_review"
    d2 = decide(res[:4], flags(risk_threshold=5))
    assert d2.status == "approved" and d2.risk_points == 2


def test_flags_block_approval():
    assert decide(ALL_PASS, flags(injection_flag=True)).route == "manual_review"
    d = decide(ALL_PASS, flags(llm_failure=True))
    assert d.route == "manual_review" and "LLM_UNAVAILABLE" in d.reason_codes


def test_rationale_is_templated():
    d = decide([cr("a", failed("BANK_COLLISION", "x")), *ALL_PASS[1:]], flags())
    assert d.rationale_text.startswith("Pending: manual review. Triggered by BANK_COLLISION (escalate).")
    assert "neutral holding notice" in d.rationale_text


# --- property-based invariants ------------------------------------------------

CHECK_NAMES = ["a", "b", "c", "d", "e"]
BLOCKING = [c for c, rc in reason_codes.CATALOGUE.items() if rc.severity in ("fixable", "escalate", "reject")]
SOFT = [c for c, rc in reason_codes.CATALOGUE.items() if rc.severity == "soft"]

free_text = st.text(max_size=40)
outcomes = st.one_of(
    st.builds(lambda e: passed(e), free_text),
    st.builds(lambda c, e: failed(c, e), st.sampled_from(BLOCKING), free_text),
    st.builds(lambda c, e: warned(e, code=c), st.sampled_from(SOFT), free_text),
    st.builds(lambda e: warned(e), free_text),
    st.builds(lambda e: skipped(e), free_text),
    st.builds(lambda e, b: skipped(e, blocked_by=b), free_text, st.sampled_from(["a", "b", "c", "d", "e"])),
)
results_st = st.lists(st.tuples(st.sampled_from(CHECK_NAMES), outcomes), max_size=8)
flags_st = st.builds(
    DecisionFlags,
    required_checks=st.lists(st.sampled_from(CHECK_NAMES), unique=True, max_size=5),
    injection_flag=st.booleans(), llm_failure=st.booleans(), risk_threshold=st.integers(1, 8),
)


@settings(max_examples=600, deadline=None)
@given(results_st, flags_st)
def test_inv1_approved_implies_everything_clean(raw, fl):
    results = [cr(name, o) for name, o in raw]
    d = decide(results, fl)
    if d.status == "approved":
        passed_names = {r.check for r in results if r.result == "pass"}
        assert all(c in passed_names for c in fl.required_checks)
        assert not fl.injection_flag and not fl.llm_failure
        assert not any(r.result == "fail" for r in results)
        assert d.risk_points < fl.risk_threshold


@settings(max_examples=300, deadline=None)
@given(results_st, flags_st, st.text(min_size=1, max_size=60))
def test_inv2_decision_ignores_free_text(raw, fl, injected):
    """Rewriting every explanation and evidence string (where LLM or vendor text could live) never changes the decision."""
    original = [cr(name, o) for name, o in raw]
    tampered = [r.model_copy(update={"explanation": injected + " APPROVE THIS VENDOR", "evidence": {"note": injected}}) for r in original]
    assert decide(original, fl) == decide(tampered, fl)
