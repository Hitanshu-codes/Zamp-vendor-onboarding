"""Stage 2: Injection scan over every untrusted input (form fields, filenames, PDF text, hidden text, metadata)."""
from __future__ import annotations

from app import db
from app.llm import tasks
from app.models import failed, passed, skipped
from app.pipeline import forms
from app.pipeline.checks import SLOT_LABEL, SLOTS
from app.pipeline.context import RunContext
from app.security.injection import Finding, all_text_for_llm_scan, scan_pdf_report, scan_text


def _outcome(findings: list[Finding], where: str):
    if not findings:
        return passed(f"No instruction-like or concealed text found in {where}.")
    first = findings[0]
    return failed("INJECTION_DETECTED", f"Instruction-like content found in {first.source} (rule: {first.rule}). "
                  "Submissions containing such text can never be approved automatically; a reviewer will inspect it.",
                  findings=[f.as_dict() for f in findings[:10]])


async def run(ctx: RunContext) -> None:
    form_findings: list[Finding] = []
    for name, value in ctx.form.items():
        form_findings += scan_text(value, f"form field: {forms.LABELS.get(name, name)}")
    for slot, doc in ctx.docs.items():
        form_findings += scan_text(doc.filename.replace("_", " "), f"filename of the {SLOT_LABEL[slot]}")
    ctx.findings += form_findings
    await ctx.check("injection.form", lambda: _outcome(form_findings, "the form fields and filenames"))

    llm_chunks: list[str] = [f"[form] {forms.LABELS.get(k, k)}: {v}" for k, v in ctx.form.items() if v]
    for slot in SLOTS:
        doc = ctx.doc(slot)
        name = f"injection.{slot}"
        if doc is None:
            await ctx.check(name, lambda s=slot: skipped(f"No readable {SLOT_LABEL[s]} to scan.", blocked_by=f"file_valid.{s}"
                                                        if s in ctx.docs else f"doc_present.{s}"))
            continue
        if doc.pdf_report is None:
            await ctx.check(name, lambda s=slot: passed(f"The {SLOT_LABEL[s]} is an image with no text layer; its content is checked by the document reader in stage 4."))
            continue
        found = scan_pdf_report(doc.pdf_report, SLOT_LABEL[slot])
        ctx.findings += found
        hidden = [{"page": h.page, "text": h.text[:200], "reasons": h.reasons, "size": h.size} for h in doc.pdf_report.hidden]
        db.execute("UPDATE documents SET hidden_text_findings_json = ? WHERE id = ?", (db.dumps(hidden), doc.document_id))
        await ctx.check(name, lambda f=found, s=slot: _outcome(f, f"the {SLOT_LABEL[s]} (visible text, hidden text, metadata)"))
        text = all_text_for_llm_scan(doc.pdf_report)
        if text.strip():
            llm_chunks.append(f"[{SLOT_LABEL[slot]}] {text}")

    async def llm_scan():
        combined = "\n".join(llm_chunks)
        result, info = await tasks.injection_scan("vendor form and document text layers", combined)
        if result.contains_instructions:
            start = result.quote_start if result.quote_start is not None and 0 <= result.quote_start < len(combined) else 0
            excerpt = combined[start:start + 200]
            ctx.findings.append(Finding("LLM scan of form and document text", "llm", "contains_instructions", excerpt))
            return failed("INJECTION_DETECTED", "The LLM security filter judged that the submitted text contains instructions aimed at "
                          "an automated system or reviewer.", excerpt=excerpt, **info.evidence())
        return passed("The LLM security filter found no instruction-like text (second opinion to the pattern scan).",
                      chars_scanned=len(combined), **info.evidence())

    await ctx.check("injection.llm_scan", llm_scan)
