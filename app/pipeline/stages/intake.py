"""Stage 1: Intake. Validate the files and the form."""
from __future__ import annotations

import pymupdf

from app.config import config
from app.models import failed, passed, skipped
from app.pipeline import checks, forms
from app.pipeline.checks import SLOT_LABEL, SLOTS
from app.pipeline.context import DocInfo, RunContext
from app.security.injection import analyse_pdf

EXECUTABLE_MAGIC = {b"MZ": "Windows executable", b"\x7fELF": "Linux executable", b"#!": "script", b"PK\x03\x04": "ZIP/Office archive",
                    b"\xca\xfe\xba\xbe": "Mach-O / Java class", b"\xcf\xfa\xed\xfe": "Mach-O executable"}


def sniff(data: bytes) -> str | None:
    """MIME type from magic bytes. The file extension and the browser's content type are ignored."""
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    return None


def validate_file(doc: DocInfo):
    label = SLOT_LABEL[doc.slot]
    ev = {"filename": doc.filename, "size_bytes": doc.size, "sha256": doc.sha256[:16]}
    for magic, kind in EXECUTABLE_MAGIC.items():
        if doc.data.startswith(magic):
            return failed("DOC_UNREADABLE", f"The {label} upload is a {kind}, not a document. Only PDF, PNG or JPEG files are accepted.", **ev)
    mime = sniff(doc.data)
    if mime is None:
        return failed("DOC_UNREADABLE", f"The {label} upload is not a PDF, PNG or JPEG file (checked by file content, not name).", **ev)
    if doc.size > config.max_upload_mb * 1024 * 1024:
        return failed("DOC_UNREADABLE", f"The {label} is larger than {config.max_upload_mb} MB.", **ev)
    doc.mime = mime
    ev["type"] = mime
    if mime != "application/pdf":
        doc.page_count, doc.valid = 1, True
        return passed(f"The {label} is a valid {mime.split('/')[1].upper()} image.", **ev)
    try:
        pdf = pymupdf.open(stream=doc.data, filetype="pdf")
    except Exception:
        return failed("DOC_UNREADABLE", f"The {label} PDF is corrupt and cannot be opened.", **ev)
    try:
        if pdf.needs_pass or pdf.is_encrypted:
            return failed("DOC_UNREADABLE", f"The {label} PDF is password-protected. Please upload an unprotected copy.", **ev)
        doc.page_count = pdf.page_count
        ev["pages"] = pdf.page_count
        if pdf.page_count == 0:
            return failed("DOC_UNREADABLE", f"The {label} PDF has no pages.", **ev)
        if pdf.page_count > config.max_pages:
            return failed("DOC_UNREADABLE", f"The {label} has {pdf.page_count} pages; the limit is {config.max_pages}.", **ev)
    finally:
        pdf.close()
    try:
        doc.pdf_report = analyse_pdf(doc.data)
    except Exception:
        return failed("DOC_UNREADABLE", f"The {label} PDF could not be parsed.", **ev)
    doc.valid = True
    return passed(f"The {label} is a readable {doc.page_count}-page PDF.", **ev)


BANK_INPUTS = {"form:bank_account_number", "form:iban", "form:bank_code", "form:bank_account_holder"}


def allowed_changes(ctx: RunContext) -> set[str]:
    """Inputs the vendor was asked to change, derived from the checks we asked them to fix."""
    allowed: set[str] = set()
    for name in ctx.requested_items:
        if name == "form_complete":
            prev = ctx.previous.get(name)
            allowed |= {f"form:{m}" for m in (prev.evidence.get("missing", []) if prev else [])}
            continue
        if name in checks.CHECKS:
            allowed |= {i for i in checks.CHECKS[name].inputs if not i.endswith(":*")}
    if "doc:bank_letter" in allowed:
        allowed |= BANK_INPUTS  # a new bank document naturally comes with new account details
    return allowed


def unrequested_change(ctx: RunContext):
    if ctx.trigger != "resubmission" or ctx.changed is None:
        return skipped("Only applies to resubmissions.")
    allowed = allowed_changes(ctx)
    extra = sorted(ctx.changed - allowed)
    ev = {"changed": sorted(ctx.changed), "requested": ctx.requested_items, "allowed_changes": sorted(allowed)}
    if extra:
        pretty = [forms.LABELS.get(x.split(":", 1)[1], x.split(":", 1)[1]) if x.startswith("form:") else SLOT_LABEL.get(x.split(":", 1)[1], x)
                  for x in extra]
        return failed("UNREQUESTED_CHANGE", f"The resubmission also changed things we did not ask for: {', '.join(pretty)}. "
                      "Unrequested changes, especially to bank details, need a person's review. All checks that depend on them were re-run.",
                      unrequested=extra, **ev)
    return passed("The resubmission only changed the items we asked for.", **ev)


def bank_change(ctx: RunContext):
    if ctx.trigger != "bank_change":
        return skipped("Not a bank change.")
    return failed("BANK_CHANGE_REQUEST", "An approved vendor asked to change where we pay them. Payments are on hold until a person verifies "
                  "the request by calling the vendor on the phone number already on file (never a number from the request itself).",
                  changed=sorted(ctx.changed or []))


async def run(ctx: RunContext) -> None:
    def form_check():
        missing = forms.missing_fields(ctx.form)
        if missing:
            labels = [forms.LABELS.get(m, m) for m in dict.fromkeys(missing)]
            return failed("FORM_FIELD_MISSING", f"Missing or invalid form fields: {', '.join(labels)}.", missing=list(dict.fromkeys(missing)))
        return passed("All required form fields are filled in.", fields_checked=len(forms.FIELDS))

    await ctx.check("form_complete", form_check)
    await ctx.check("unrequested_change", lambda: unrequested_change(ctx))
    await ctx.check("bank_change_request", lambda: bank_change(ctx))
    for slot in SLOTS:
        doc = ctx.docs.get(slot)
        name = f"file_valid.{slot}"
        if doc is None:
            await ctx.check(name, lambda s=slot: skipped(f"No {SLOT_LABEL[s]} was uploaded.", blocked_by=f"doc_present.{s}"))
        else:
            # Always establish the file's facts (validity, text layer); reuse only affects which result is reported.
            outcome = validate_file(doc)
            await ctx.check(name, lambda o=outcome: o)
