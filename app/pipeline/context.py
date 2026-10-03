"""Per-run state shared by the stages, and the single place where check results are recorded."""
from __future__ import annotations

import inspect
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from app import db, events
from app.domain.taxids import TaxRule
from app.llm.client import CallInfo, LLMUnavailable
from app.llm.schemas import DocumentExtraction
from app.models import CheckOutcome, CheckResult, Decision, failed
from app.pipeline import checks
from app.security.injection import Finding, PdfTextReport


@dataclass
class DocInfo:
    slot: str
    document_id: int
    filename: str
    sha256: str
    mime: str
    size: int
    data: bytes
    page_count: int = 0
    valid: bool = False
    invalid_reason: str | None = None
    pdf_report: PdfTextReport | None = None
    extraction: DocumentExtraction | None = None
    extraction_info: CallInfo | None = None
    extraction_error: str | None = None
    extraction_reused: bool = False

    def page_of(self, value: str | None) -> int | None:
        """First page whose text contains the value (for 'source document and page' evidence)."""
        if not value or not self.pdf_report:
            return None
        needle = "".join(value.lower().split())
        for i, text in enumerate(self.pdf_report.visible_text, start=1):
            if needle and needle in "".join(text.lower().split()):
                return i
        return None


@dataclass
class RunContext:
    run_id: int
    submission_id: int
    version_id: int
    version_no: int
    trigger: str
    form: dict[str, str]
    docs: dict[str, DocInfo] = field(default_factory=dict)
    results: dict[str, CheckResult] = field(default_factory=dict)
    previous: dict[str, CheckResult] = field(default_factory=dict)
    previous_version_no: int | None = None
    changed: set[str] | None = None            # None on an initial run: everything runs
    requested_items: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    llm_failures: list[str] = field(default_factory=list)
    tax_rule: TaxRule | None = None
    tax_type: str = ""
    decision: Decision | None = None
    stage: str = "intake"

    # --- flags read by decide() ------------------------------------------------
    @property
    def injection_flag(self) -> bool:
        return any(r.reason_code == "INJECTION_DETECTED" for r in self.results.values())

    @property
    def llm_failure(self) -> bool:
        return bool(self.llm_failures)

    # --- helpers -----------------------------------------------------------------
    def doc(self, slot: str) -> DocInfo | None:
        d = self.docs.get(slot)
        return d if d and d.valid else None

    def extraction(self, slot: str) -> DocumentExtraction | None:
        d = self.doc(slot)
        return d.extraction if d else None

    def usable(self, slot: str) -> tuple[DocumentExtraction | None, str | None]:
        """Extraction from a document whose type check passed, else (None, name of the check that blocks it)."""
        if slot not in self.docs:
            return None, f"doc_present.{slot}"
        if not self.docs[slot].valid:
            return None, f"file_valid.{slot}"
        r = self.results.get(f"doc_type.{slot}")
        if r is None or r.result != "pass":
            return None, f"doc_type.{slot}"
        return self.docs[slot].extraction, None

    def failed_check(self, name: str) -> bool:
        r = self.results.get(name)
        return r is not None and r.result == "fail"

    def _reusable(self, name: str) -> CheckResult | None:
        if self.changed is None:
            return None
        spec = checks.spec(name)
        prev = self.previous.get(name)
        if spec.always_rerun or prev is None or prev.result not in ("pass", "warn"):
            return None
        if checks.depends_on_changed(name, self.changed):
            return None
        return prev

    async def check(self, name: str, compute: Callable[[], CheckOutcome | Awaitable[CheckOutcome]]) -> CheckResult:
        """Run (or reuse) one check, then persist and stream its result."""
        spec = checks.spec(name)
        prev = self._reusable(name)
        if prev is not None:
            result = prev.model_copy(update={"reused_from_run_id": prev.reused_from_run_id or -1})
            reused_version = self.previous_version_no
        else:
            try:
                outcome = compute()
                if inspect.isawaitable(outcome):
                    outcome = await outcome
            except LLMUnavailable as exc:
                self.llm_failures.append(f"{name}: {exc.reason}")
                outcome = failed("LLM_UNAVAILABLE", f"The LLM could not be used for this check ({exc.reason}). "
                                 "Failing closed: a person will review instead.", llm_task=exc.task, llm_error=exc.reason)
            result = CheckResult.of(name, spec.stage, outcome, required=spec.required(self.form))
            reused_version = None
        self.results[name] = result
        self._persist(result, reused_version)
        return result

    def _persist(self, r: CheckResult, reused_version: int | None) -> None:
        db.execute(
            "INSERT INTO check_results(run_id, stage, check_name, result, severity, reason_code, risk_points, evidence_json,"
            " explanation, blocked_by, required, reused_from_run_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.run_id, r.stage, r.check, r.result, r.severity, r.reason_code, r.risk_points, db.dumps(r.evidence),
             r.explanation, r.blocked_by, int(r.required), None if reused_version is None else reused_version),
        )
        events.emit(self.run_id, r.stage, "check_result", {
            "check": r.check, "label": checks.spec(r.check).label, "result": r.result, "severity": r.severity,
            "reason_code": r.reason_code, "risk_points": r.risk_points, "explanation": r.explanation, "evidence": r.evidence,
            "required": r.required, "blocked_by": r.blocked_by, "reused_from_version": reused_version,
        })

    def info(self, message: str, **data: Any) -> None:
        events.emit(self.run_id, self.stage, "info", {"message": message, **data})


StageFn = Callable[[RunContext], Awaitable[None]]


async def run_stage(ctx: RunContext, key: str, fn: StageFn) -> None:
    ctx.stage = key
    events.emit(ctx.run_id, key, "stage_started", {})
    start = time.perf_counter()
    before = set(ctx.results)
    error: str | None = None
    try:
        await fn(ctx)
    except Exception as exc:  # fail closed: an unexpected error can never lead to approval
        error = f"{type(exc).__name__}: {exc}"[:300]
        # Checks this stage should have produced are now missing; decide() treats missing required checks as CHECK_MISSING.
        events.emit(ctx.run_id, key, "error", {"message": "This stage failed unexpectedly; the submission will go to manual review.", "error": error})
    new = [ctx.results[n] for n in ctx.results if n not in before]
    counts = {k: sum(1 for r in new if r.result == k) for k in ("pass", "fail", "warn", "skipped")}
    events.emit(ctx.run_id, key, "stage_finished", {
        "duration_ms": int((time.perf_counter() - start) * 1000),
        "counts": counts,
        "reused": sum(1 for r in new if r.reused_from_run_id),
        "state": "failed" if error or counts["fail"] else ("warn" if counts["warn"] else "done"),
        "error": error,
    })
