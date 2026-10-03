"""Vendor-facing asks per fixable reason code.

Only enumerated values are interpolated: slot labels, field labels, document-type labels,
country names and system-formatted dates. Never free text from the vendor or the LLM (INV-3).
"""
from __future__ import annotations

from app.pipeline import forms
from app.pipeline.checks import SLOT_LABEL
from app.pipeline.stages.classify_extract import TYPE_LABEL

TAX_DOC_NAME = {"IN": "GST registration certificate", "US": "EIN confirmation letter", "GB": "VAT registration certificate",
                "DE": "VAT (USt-IdNr.) certificate", "SG": "ACRA business profile showing your UEN"}


def _slot_of(check: str) -> str | None:
    tail = check.split(".", 1)[1] if "." in check else ""
    return tail if tail in SLOT_LABEL else None


def ask_for(code: str, check: str, evidence: dict, country: str) -> str:
    slot = _slot_of(check)
    doc = SLOT_LABEL.get(slot or "", "document")
    if code == "FORM_FIELD_MISSING":
        labels = [forms.LABELS[m] for m in evidence.get("missing", []) if m in forms.LABELS]
        return f"Please complete the following form fields: {', '.join(labels) or 'the highlighted fields'}."
    if code == "DOC_MISSING":
        return f"Please upload your {doc}."
    if code == "DOC_WRONG_TYPE":
        found = TYPE_LABEL.get(evidence.get("doc_type", ""), "a different document")
        wanted = TAX_DOC_NAME.get(country, "tax registration certificate") if slot == "tax_cert" else TYPE_LABEL.get(evidence.get("expected", ""), doc)
        return f"We received what appears to be a {found} where the {doc} was expected. Please upload your {wanted}."
    if code == "DOC_UNREADABLE":
        return f"We could not read your {doc}. Please upload a clear, complete PDF (not password-protected) or a sharp photo."
    if code == "DOC_EXPIRED":
        return f"Your {doc} expired on {evidence.get('expiry_date', 'an earlier date')}. Please upload a currently valid copy."
    if code == "DOC_STALE":
        return (f"Your bank letter is dated {evidence.get('issue_date', 'too long ago')}. Please provide one issued within the last "
                f"{evidence.get('max_age_days', 90)} days.")
    if code == "TAXID_CHECKSUM_FAIL":
        return "Your tax ID appears to contain a typo. Please check it against your certificate and re-enter it."
    if code == "TAXID_FORMAT_FAIL":
        return (f"Your tax ID does not match the expected format for {forms.COUNTRIES.get(country, country)}. "
                "Please check it against your certificate and re-enter it.")
    if code == "NAME_MISMATCH_CLEAR":
        return "The company name on the form differs from the name on your documents. Please make sure the form shows your exact registered legal name."
    if code == "BANK_HOLDER_NOT_COMPANY":
        return ("For company vendors we pay into an account held in the company's name. Please provide a bank letter or cancelled "
                "cheque for a company account, or, if payments should go to this account, a signed authorisation letter on company letterhead.")
    if code == "BANK_DETAILS_INVALID":
        return "Your bank account number or bank code appears to be mistyped. Please check and re-enter them."
    return "Please review and correct the highlighted information."
