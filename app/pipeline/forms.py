"""The vendor form: field definitions and normalisation."""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass

COUNTRIES = {
    "IN": "India", "US": "United States", "GB": "United Kingdom", "DE": "Germany", "SG": "Singapore", "CL": "Chile",
    "BR": "Brazil", "FR": "France", "NL": "Netherlands", "AE": "United Arab Emirates", "AU": "Australia", "CA": "Canada",
}
COUNTRY_ALIASES = {name.lower(): code for code, name in COUNTRIES.items()} | {
    "usa": "US", "united states of america": "US", "uk": "GB", "england": "GB", "great britain": "GB", "bharat": "IN", "uae": "AE",
}
ENTITY_TYPES = {"company": "Company", "sole_proprietor": "Sole proprietor", "partnership": "Partnership"}
CATEGORIES = {"goods": "Goods supplier", "services": "Services", "logistics": "Logistics", "software": "Software / SaaS",
              "international_services": "International services (foreign account allowed)"}
FOREIGN_ACCOUNT_EXEMPT = {"international_services"}


@dataclass(frozen=True)
class Field:
    name: str
    label: str
    required: bool = True


FIELDS = [
    Field("legal_name", "Legal name"),
    Field("trade_name", "Trade name", required=False),
    Field("country", "Country of registration"),
    Field("entity_type", "Entity type"),
    Field("registered_address", "Registered address"),
    Field("contact_name", "Contact name"),
    Field("contact_email", "Contact email"),
    Field("tax_id", "Tax ID"),
    Field("tax_id_type", "Tax ID type", required=False),
    Field("bank_account_holder", "Bank account holder"),
    Field("bank_account_number", "Bank account number", required=False),  # either this or IBAN
    Field("iban", "IBAN", required=False),
    Field("bank_code", "Bank code (IFSC / SWIFT / ABA)"),
    Field("vendor_category", "Vendor category"),
]
FIELD_NAMES = [f.name for f in FIELDS]
LABELS = {f.name: f.label for f in FIELDS}


def _clean(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    return " ".join(text.split())[:300]


def normalize_country(value: str) -> str:
    v = _clean(value)
    if len(v) == 2 and v.isalpha():
        return v.upper()
    return COUNTRY_ALIASES.get(v.lower(), v.upper()[:2] if v else "")


def normalize_form(raw: dict) -> dict[str, str]:
    form = {name: _clean(raw.get(name, "")) for name in FIELD_NAMES}
    form["country"] = normalize_country(form["country"])
    form["entity_type"] = form["entity_type"].lower().replace(" ", "_")
    form["contact_email"] = form["contact_email"].lower()
    form["tax_id"] = form["tax_id"].upper().replace(" ", "")
    form["tax_id_type"] = form["tax_id_type"].upper()
    form["iban"] = form["iban"].upper().replace(" ", "")
    form["bank_code"] = form["bank_code"].upper().replace(" ", "")
    form["bank_account_number"] = form["bank_account_number"].replace(" ", "")
    return form


def missing_fields(form: dict[str, str]) -> list[str]:
    missing = [f.name for f in FIELDS if f.required and not form.get(f.name)]
    if not form.get("bank_account_number") and not form.get("iban"):
        missing.append("bank_account_number")
    if form.get("entity_type") and form["entity_type"] not in ENTITY_TYPES:
        missing.append("entity_type")
    if form.get("contact_email") and "@" not in form["contact_email"]:
        missing.append("contact_email")
    return missing


def account(form: dict[str, str]) -> str:
    return form.get("iban") or form.get("bank_account_number") or ""
