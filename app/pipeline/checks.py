"""Check registry: every check's label, stage, inputs, and whether approval requires it.

Inputs drive partial re-runs. On a resubmission a check is re-run if it did not
pass last time or if any of its inputs changed; otherwise its previous result is
reused and marked "reused from vN". Inputs are "form:<field>", "doc:<slot>",
"form:*" (any field) or "doc:*" (any document).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

SLOTS = ("tax_cert", "bank_letter", "incorporation_cert")
SLOT_LABEL = {"tax_cert": "tax certificate", "bank_letter": "bank letter", "incorporation_cert": "incorporation certificate"}
BANK_FIELDS = ["form:bank_account_number", "form:iban", "form:bank_code"]


def _always(form: dict) -> bool:
    return True


def _needs_incorporation(form: dict) -> bool:
    return form.get("entity_type") != "sole_proprietor"


def _india(form: dict) -> bool:
    return form.get("country") == "IN" and (form.get("tax_id_type") or "GSTIN") == "GSTIN"


@dataclass(frozen=True)
class CheckSpec:
    label: str
    stage: str
    inputs: list[str]
    required: Callable[[dict], bool] = _always
    always_rerun: bool = False  # risk lists and injection scans change over time; cheap to repeat
    slots: tuple[str, ...] = field(default_factory=tuple)


_SPECS: dict[str, CheckSpec] = {
    "form_complete": CheckSpec("Required form fields are filled in", "intake", ["form:*"]),
    "unrequested_change": CheckSpec("Resubmission only changed what we asked for", "intake", ["form:*", "doc:*"],
                                    required=lambda f: False, always_rerun=True),
    "file_valid.{slot}": CheckSpec("File is a readable PDF or image", "intake", ["doc:{slot}"], slots=SLOTS),
    "injection.form": CheckSpec("No instructions hidden in form fields", "injection_scan", ["form:*"], always_rerun=True),
    "injection.{slot}": CheckSpec("No instructions or hidden text in the file", "injection_scan", ["doc:{slot}"], always_rerun=True, slots=SLOTS),
    "injection.llm_scan": CheckSpec("LLM second opinion: no instruction-like text", "injection_scan", ["form:*", "doc:*"], always_rerun=True),
    "doc_present.{slot}": CheckSpec("Document provided", "completeness", ["doc:{slot}", "form:entity_type"], slots=SLOTS),
    "doc_type.{slot}": CheckSpec("Document content matches the slot", "classify_extract", ["doc:{slot}"], slots=SLOTS),
    "doc_quality.{slot}": CheckSpec("Document quality", "classify_extract", ["doc:{slot}"], required=lambda f: False, slots=SLOTS),
    "doc_instructions.{slot}": CheckSpec("Document reader saw no instructions", "classify_extract", ["doc:{slot}"], always_rerun=True, slots=SLOTS),
    "tax_rule": CheckSpec("A format rule exists for this tax ID", "tax_rule_lookup", ["form:country", "form:tax_id_type"]),
    "tax_id_format": CheckSpec("Tax ID format and checksum", "validate", ["form:tax_id", "form:country", "form:tax_id_type"]),
    "tax_id_matches_cert": CheckSpec("Tax ID on the form matches the certificate", "validate", ["form:tax_id", "doc:tax_cert"]),
    "gstin_pan": CheckSpec("GSTIN contains the PAN shown on the documents", "validate", ["form:tax_id", "doc:tax_cert"], required=_india),
    "pan_entity": CheckSpec("PAN holder type matches the entity type", "validate", ["form:tax_id", "form:entity_type"], required=_india),
    "bank_format": CheckSpec("Bank account and code are well-formed", "validate", [*BANK_FIELDS, "form:country"]),
    "bank_country": CheckSpec("Bank is in the vendor's country", "validate", [*BANK_FIELDS, "form:country", "form:vendor_category"]),
    "doc_expiry.{slot}": CheckSpec("Document is not expired", "validate", ["doc:{slot}"], required=lambda f: False, slots=("tax_cert", "incorporation_cert")),
    "bank_letter_age": CheckSpec("Bank letter is recent", "validate", ["doc:bank_letter"], required=lambda f: False),
    "date_order": CheckSpec("Registration dates are in a possible order", "validate", ["doc:tax_cert", "doc:incorporation_cert"], required=lambda f: False),
    "contact_email": CheckSpec("Contact email domain", "validate", ["form:contact_email", "form:entity_type"], required=lambda f: False),
    "name.tax_cert": CheckSpec("Legal name matches the tax certificate", "cross_reference", ["form:legal_name", "doc:tax_cert"]),
    "name.incorporation_cert": CheckSpec("Legal name matches the incorporation certificate", "cross_reference",
                                         ["form:legal_name", "doc:incorporation_cert"], required=_needs_incorporation),
    "name.bank_holder": CheckSpec("Account holder on the form matches the bank document", "cross_reference",
                                  ["form:bank_account_holder", "doc:bank_letter"]),
    "bank_matches_letter": CheckSpec("Account number on the form matches the bank document", "cross_reference",
                                     ["form:bank_account_number", "form:iban", "doc:bank_letter"]),
    "holder_type": CheckSpec("Account belongs to the business, not an individual", "cross_reference",
                             ["form:legal_name", "form:entity_type", "form:bank_account_holder", "doc:bank_letter", "doc:incorporation_cert", "doc:tax_cert"]),
    "country_consistency": CheckSpec("Country agrees across form, documents and bank", "cross_reference",
                                     ["form:country", "form:registered_address", *BANK_FIELDS, "doc:*"]),
    "sanctions": CheckSpec("Sanctions screening (MOCK-LIST-A)", "risk_screen",
                           ["form:legal_name", "form:trade_name", "form:bank_account_holder", "doc:*"], always_rerun=True),
    "watchlist": CheckSpec("Internal watchlist screening", "risk_screen", ["form:*", "doc:*"], always_rerun=True),
    "duplicate_vendor": CheckSpec("Not already an active vendor", "risk_screen", ["form:tax_id"], always_rerun=True),
    "bank_collision": CheckSpec("Bank account not used by another vendor", "risk_screen",
                                ["form:bank_account_number", "form:iban", "form:legal_name"], always_rerun=True),
}


def _expand() -> dict[str, CheckSpec]:
    out: dict[str, CheckSpec] = {}
    for name, spec in _SPECS.items():
        if "{slot}" not in name:
            out[name] = spec
            continue
        for slot in spec.slots:
            required = spec.required
            if name.startswith(("file_valid", "doc_present", "doc_type", "doc_instructions", "injection.")) and slot == "incorporation_cert":
                required = _needs_incorporation
            out[name.replace("{slot}", slot)] = CheckSpec(
                f"{spec.label} ({SLOT_LABEL[slot]})", spec.stage, [i.replace("{slot}", slot) for i in spec.inputs],
                required, spec.always_rerun,
            )
    return out


CHECKS: dict[str, CheckSpec] = _expand()


def spec(name: str) -> CheckSpec:
    return CHECKS[name]


def required_checks(form: dict) -> list[str]:
    return [name for name, s in CHECKS.items() if s.required(form)]


def depends_on_changed(name: str, changed: set[str]) -> bool:
    """changed: e.g. {"form:bank_account_number", "doc:tax_cert"}."""
    for inp in CHECKS[name].inputs:
        if inp in changed:
            return True
        if inp == "form:*" and any(c.startswith("form:") for c in changed):
            return True
        if inp == "doc:*" and any(c.startswith("doc:") for c in changed):
            return True
    return False
