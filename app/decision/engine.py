"""The decision engine: a pure function from check results to a status.

No LLM output reaches this module except as enumerated check results. The
rationale is rendered from reason codes, never from free text (INV-2).
"""
from __future__ import annotations

from app.decision.reason_codes import SEVERITY_RANK, get
from app.models import CheckResult, Decision, DecisionFlags, TriggeredCode

_PRECEDENCE = {
    "rejected": "A reject-level finding outranks everything else, so the submission is closed. The vendor gets a neutral notice with no reason.",
    "manual_review": "At least one finding needs a person's judgement. Manual review outranks asking the vendor for fixes, so the vendor gets a neutral holding notice and the reviewer sees any fixable items too.",
    "vendor_followup": "Every finding is something the vendor can fix and none is suspicious, so we send one consolidated request listing exactly what to correct.",
    "approved": "Every required check passed, nothing was flagged, and the risk score is below the review threshold.",
}


def decide(results: list[CheckResult], flags: DecisionFlags) -> Decision:
    triggered: list[TriggeredCode] = []
    seen: set[tuple[str, str]] = set()
    for r in results:
        if r.reason_code and r.result in ("fail", "warn") and (r.reason_code, r.check) not in seen:
            seen.add((r.reason_code, r.check))
            triggered.append(TriggeredCode(code=r.reason_code, severity=r.severity, check=r.check, risk_points=r.risk_points))

    risk_points = sum(t.risk_points for t in triggered)

    # A required check counts only if it is present and passed. Anything else is a failure.
    passed_checks = {r.check for r in results if r.result == "pass"}
    blocking_checks = {r.check for r in results if r.result == "fail"}
    missing_required = [c for c in flags.required_checks if c not in passed_checks]
    for check in missing_required:
        if check not in blocking_checks:  # absent, skipped, or warned without a blocking code
            triggered.append(TriggeredCode(code="CHECK_MISSING", severity="escalate", check=check))

    if flags.injection_flag and not any(t.code == "INJECTION_DETECTED" for t in triggered):
        triggered.append(TriggeredCode(code="INJECTION_DETECTED", severity="escalate", check="injection_flag"))
    if flags.llm_failure and not any(t.code == "LLM_UNAVAILABLE" for t in triggered):
        triggered.append(TriggeredCode(code="LLM_UNAVAILABLE", severity="escalate", check="llm_failure"))

    severities = {t.severity for t in triggered}
    over_threshold = risk_points >= flags.risk_threshold

    if "reject" in severities:
        status, route, message, key = "rejected", "none", "closure", "rejected"
    elif "escalate" in severities or over_threshold:
        status, route, message, key = "pending", "manual_review", "holding", "manual_review"
    elif "fixable" in severities:
        status, route, message, key = "pending", "vendor_followup", "request", "vendor_followup"
    else:
        status, route, message, key = "approved", "none", "approval", "approved"

    triggered.sort(key=lambda t: (-SEVERITY_RANK[t.severity], t.code))
    return Decision(
        status=status,
        route=route,
        triggered=triggered,
        risk_points=risk_points,
        missing_required=missing_required,
        vendor_message=message,
        rationale_text=_rationale(status, route, message, triggered, risk_points, flags.risk_threshold, over_threshold),
        precedence_explanation=_PRECEDENCE[key],
    )


_MESSAGE_LABEL = {
    "approval": "approval notice",
    "request": "consolidated request for corrections",
    "holding": "neutral holding notice",
    "closure": "neutral closure notice",
}


def _rationale(status: str, route: str, message: str, triggered: list[TriggeredCode], points: int, threshold: int, over: bool) -> str:
    head = {"approved": "Approved.", "rejected": "Rejected.", "pending": f"Pending: {route.replace('_', ' ')}."}[status]
    parts = [head]
    blocking = [t for t in triggered if t.severity in ("fixable", "escalate", "reject")]
    if blocking:
        listed = ", ".join(f"{t.code} ({t.severity})" for t in blocking)
        parts.append(f"Triggered by {listed}.")
    soft = [t for t in triggered if t.severity == "soft"]
    if soft:
        listed = ", ".join(f"{t.code} (+{t.risk_points})" for t in soft)
        parts.append(f"Risk points {points}/{threshold} from {listed}{'; threshold reached' if over else ''}.")
    if status == "approved" and not soft:
        parts.append("All required checks passed with no findings.")
    parts.append(f"Vendor-facing message: {_MESSAGE_LABEL[message]}.")
    for t in blocking[:1]:
        parts.append(f"Main reason: {get(t.code).meaning}")
    return " ".join(parts)
