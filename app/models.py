"""Core value types shared by checks, the decision engine and the pipeline."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from app.decision import reason_codes

Result = Literal["pass", "fail", "warn", "skipped"]
Severity = Literal["info", "soft", "fixable", "escalate", "reject"]
Status = Literal["approved", "pending", "rejected"]
Route = Literal["none", "vendor_followup", "manual_review"]


class CheckOutcome(BaseModel):
    """What a domain check concluded. Severity and risk points are filled from the catalogue."""

    result: Result
    severity: Severity = "info"
    reason_code: str | None = None
    risk_points: int = 0
    explanation: str
    evidence: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _consistent(self) -> "CheckOutcome":
        if self.reason_code is not None:
            rc = reason_codes.get(self.reason_code)
            self.severity = rc.severity
            self.risk_points = rc.risk_points
        if self.result == "fail" and self.severity not in ("fixable", "escalate", "reject"):
            raise ValueError("a failing check needs a fixable/escalate/reject reason code")
        if self.result == "pass" and self.reason_code is not None:
            raise ValueError("a passing check carries no reason code")
        if self.result in ("warn", "skipped") and self.severity in ("fixable", "escalate", "reject"):
            raise ValueError("warn/skipped results cannot carry a blocking severity")
        return self


def passed(explanation: str, **evidence: Any) -> CheckOutcome:
    return CheckOutcome(result="pass", explanation=explanation, evidence=evidence)


def failed(code: str, explanation: str, **evidence: Any) -> CheckOutcome:
    return CheckOutcome(result="fail", reason_code=code, explanation=explanation, evidence=evidence)


def warned(explanation: str, code: str | None = None, **evidence: Any) -> CheckOutcome:
    return CheckOutcome(result="warn", reason_code=code, explanation=explanation, evidence=evidence)


def skipped(explanation: str, **evidence: Any) -> CheckOutcome:
    return CheckOutcome(result="skipped", explanation=explanation, evidence=evidence)


class CheckResult(CheckOutcome):
    check: str
    stage: str
    required: bool = False
    reused_from_run_id: int | None = None
    inputs_hash: str | None = None

    @classmethod
    def of(cls, check: str, stage: str, outcome: CheckOutcome, required: bool = False) -> "CheckResult":
        return cls(check=check, stage=stage, required=required, **outcome.model_dump())


class DecisionFlags(BaseModel):
    required_checks: list[str]
    injection_flag: bool = False
    llm_failure: bool = False
    risk_threshold: int = 5


class TriggeredCode(BaseModel):
    code: str
    severity: Severity
    check: str
    risk_points: int = 0


VendorMessage = Literal["approval", "request", "holding", "closure"]


class Decision(BaseModel):
    """Every field is an enum, a catalogue code, a number, or text rendered from a fixed template (INV-2)."""

    status: Status
    route: Route
    triggered: list[TriggeredCode]
    risk_points: int
    missing_required: list[str]
    vendor_message: VendorMessage
    rationale_text: str
    precedence_explanation: str

    @property
    def reason_codes(self) -> list[str]:
        return [t.code for t in self.triggered]
