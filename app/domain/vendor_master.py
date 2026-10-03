"""Checks against already-onboarded vendors: duplicate tax ID and bank collision."""
from __future__ import annotations

from dataclasses import dataclass

from app import db
from app.domain.banking import normalize_account
from app.domain.names import normalize_name
from app.domain.taxids import normalize_tax_id
from app.models import CheckOutcome, failed, passed


@dataclass
class VendorRecord:
    id: int
    legal_name: str
    country: str
    tax_id: str | None
    bank_account: str | None
    status: str


def load_vendors() -> list[VendorRecord]:
    return [VendorRecord(r["id"], r["legal_name"], r["country"], r["tax_id"], r["bank_account"], r["status"])
            for r in db.query("SELECT * FROM vendors")]


def duplicate_outcome(tax_id: str, vendors: list[VendorRecord], exclude_vendor_id: int | None = None) -> CheckOutcome:
    t = normalize_tax_id(tax_id)
    for v in vendors:
        if v.id != exclude_vendor_id and v.status == "active" and v.tax_id and normalize_tax_id(v.tax_id) == t:
            return failed("DUPLICATE_VENDOR", f"Tax ID {t} already belongs to active vendor #{v.id} ('{v.legal_name}').",
                          existing_vendor_id=v.id, existing_vendor_name=v.legal_name, tax_id=t)
    return passed("No active vendor has this tax ID.", tax_id=t)


def collision_outcome(account: str, legal_name: str, vendors: list[VendorRecord], exclude_vendor_id: int | None = None) -> CheckOutcome:
    acc = normalize_account(account)
    me = normalize_name(legal_name)
    for v in vendors:
        if v.id == exclude_vendor_id or not v.bank_account or normalize_account(v.bank_account) != acc:
            continue
        if normalize_name(v.legal_name) == me:
            continue  # same entity; duplicate detection handles re-registration
        return failed("BANK_COLLISION", f"This bank account is already registered to a different vendor, #{v.id} ('{v.legal_name}', {v.status}). "
                      "Two companies sharing one account is a common invoice-fraud pattern.",
                      existing_vendor_id=v.id, existing_vendor_name=v.legal_name, existing_status=v.status, account=acc)
    return passed("This bank account is not registered to any other vendor.", account=acc)
