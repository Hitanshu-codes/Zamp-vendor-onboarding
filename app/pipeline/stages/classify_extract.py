"""Stage 4: Classify and extract each document with the LLM (schema-constrained, content only)."""
from __future__ import annotations

import asyncio

from app import db
from app.llm import tasks
from app.llm.client import LLMUnavailable
from app.llm.schemas import DocumentExtraction
from app.models import failed, passed, skipped, warned
from app.pipeline.checks import SLOT_LABEL, SLOTS
from app.pipeline.context import DocInfo, RunContext

EXPECTED_TYPE = {"tax_cert": "tax_registration_certificate", "bank_letter": "bank_letter_or_cheque",
                 "incorporation_cert": "incorporation_certificate"}
TYPE_LABEL = {
    "tax_registration_certificate": "tax registration certificate", "bank_letter_or_cheque": "bank letter or cancelled cheque",
    "incorporation_certificate": "certificate of incorporation", "utility_bill": "utility bill", "invoice": "invoice",
    "id_document": "personal ID document", "other": "unrelated document", "unreadable": "unreadable document",
}
KEY_FIELDS = ["legal_name", "trade_name", "tax_id", "secondary_tax_id", "issue_date", "expiry_date", "registration_date",
              "incorporation_date", "country", "account_holder", "account_number", "iban", "bank_code", "account_type"]


def _reuse_previous_extraction(doc: DocInfo) -> bool:
    """Same file bytes as an earlier version: reuse its extraction instead of calling the LLM again."""
    row = db.query_one(
        "SELECT d.extraction_json FROM documents d JOIN submission_versions v ON v.id = d.version_id"
        " WHERE d.sha256 = ? AND d.slot = ? AND d.extraction_json IS NOT NULL AND d.id != ? ORDER BY d.id DESC LIMIT 1",
        (doc.sha256, doc.slot, doc.document_id),
    )
    if row is None:
        return False
    doc.extraction = DocumentExtraction.model_validate_json(row["extraction_json"])
    doc.extraction_reused = True
    return True


async def _extract(doc: DocInfo) -> None:
    if _reuse_previous_extraction(doc):
        return
    try:
        doc.extraction, doc.extraction_info = await tasks.classify_extract(doc.data, doc.mime, doc.slot)
    except LLMUnavailable as exc:
        doc.extraction_error = exc.reason
        return
    db.execute("UPDATE documents SET classified_type = ?, extraction_json = ?, extraction_quality = ? WHERE id = ?",
               (doc.extraction.doc_type, doc.extraction.model_dump_json(), doc.extraction.quality, doc.document_id))


async def run(ctx: RunContext) -> None:
    valid = [ctx.docs[s] for s in SLOTS if ctx.doc(s)]
    await asyncio.gather(*(_extract(d) for d in valid))

    for slot in SLOTS:
        doc = ctx.doc(slot)
        label = SLOT_LABEL[slot]
        if doc is None:
            blocker = f"file_valid.{slot}" if slot in ctx.docs else f"doc_present.{slot}"
            for name in (f"doc_type.{slot}", f"doc_quality.{slot}", f"doc_instructions.{slot}"):
                await ctx.check(name, lambda b=blocker, l=label: skipped(f"No readable {l} to classify.", blocked_by=b))
            continue

        def type_outcome(d=doc, s=slot, l=label):
            if d.extraction is None:
                raise LLMUnavailable(d.extraction_error or "no extraction", "classify_extract")
            ex = d.extraction
            fields = {k: getattr(ex, k) for k in KEY_FIELDS if getattr(ex, k) not in (None, "", "unknown")}
            pages = {k: d.page_of(str(v)) for k, v in fields.items() if d.page_of(str(v))}
            ev = {"doc_type": ex.doc_type, "type_confidence": ex.type_confidence, "quality": ex.quality, "fields": fields,
                  "field_pages": pages, "directors_or_owners": ex.directors_or_owners, "language": ex.language,
                  "translation": ex.translation_of_key_fields, "extraction_reused": d.extraction_reused,
                  **(d.extraction_info.evidence() if d.extraction_info else {"llm_source": "reused from earlier version", "schema_validated": True})}
            if ex.doc_type == "unreadable" or ex.quality == "poor":
                return failed("DOC_UNREADABLE", f"The {l} could not be read reliably. Please upload a clearer copy.", **ev)
            if ex.doc_type != EXPECTED_TYPE[s]:
                return failed("DOC_WRONG_TYPE", f"We received what looks like a {TYPE_LABEL[ex.doc_type]} in the {l} slot; we need a "
                              f"{TYPE_LABEL[EXPECTED_TYPE[s]]}. (Judged from the content, not the file name.)", expected=EXPECTED_TYPE[s], **ev)
            return passed(f"Content is a {TYPE_LABEL[ex.doc_type]} (confidence {ex.type_confidence:.0%}); {len(fields)} fields extracted.", **ev)

        type_result = await ctx.check(f"doc_type.{slot}", type_outcome)

        def quality_outcome(d=doc, l=label):
            if type_result.result == "fail":
                return skipped("Not assessed: the document type check failed.", blocked_by=f"doc_type.{slot}")
            if d.extraction.quality == "degraded":
                return warned(f"The {l} is readable but low quality; its values are treated as lower trust (+1 risk point).", code="DEGRADED_DOCUMENT")
            return passed(f"The {l} is clear and legible.")

        await ctx.check(f"doc_quality.{slot}", quality_outcome)

        def instructions_outcome(d=doc, l=label):
            if d.extraction is None:
                return skipped("Not assessed: the document reader was unavailable.", blocked_by=f"doc_type.{slot}")
            if d.extraction.contains_instructions:
                return failed("INJECTION_DETECTED", f"While reading the {l}, the document reader reported text addressed to an AI, "
                              "system or reviewer. A person must inspect it.", reader_notes=d.extraction.notes)
            return passed(f"The document reader saw no instructions in the {l}.")

        await ctx.check(f"doc_instructions.{slot}", instructions_outcome)
