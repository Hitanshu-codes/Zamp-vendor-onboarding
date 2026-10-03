"""Tax ID validators and the interpretation of a validation failure.

Learned (web) rules may only reference checksum algorithms named in CHECKSUMS;
no code from the web is ever executed.
"""
from __future__ import annotations

from dataclasses import dataclass

import regex

from app.models import CheckOutcome, failed, passed

GSTIN_CHARSET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
REGEX_TIMEOUT_SECONDS = 0.5
ALNUM = GSTIN_CHARSET

# PAN 4th character: holder type.
PAN_ENTITY_CODES = {
    "P": "individual",
    "C": "company",
    "H": "hindu_undivided_family",
    "F": "firm",          # partnership firm / LLP
    "A": "association_of_persons",
    "T": "trust",
    "B": "body_of_individuals",
    "L": "local_authority",
    "J": "artificial_juridical_person",
    "G": "government",
}

# Which PAN codes are consistent with each declared entity type.
PAN_CODES_FOR_ENTITY = {
    "company": {"C"},
    "sole_proprietor": {"P"},
    "partnership": {"F"},
}

# Default tax type when the form doesn't say which one it is.
PRIMARY_TAX_TYPE = {
    "IN": "GSTIN", "US": "EIN", "DE": "VAT", "GB": "VAT", "SG": "UEN", "BR": "CNPJ", "CL": "RUT",
    "FR": "VAT", "NL": "VAT", "ES": "VAT", "IT": "VAT", "AU": "ABN", "CA": "BN",
}


def gstin_check_char(first14: str) -> str:
    """Published GSTIN mod-36 check character over the first 14 characters."""
    total = 0
    for i, ch in enumerate(first14):
        value = GSTIN_CHARSET.index(ch)
        product = value * (2 if i % 2 else 1)
        total += product // 36 + product % 36
    return GSTIN_CHARSET[(36 - total % 36) % 36]


def gstin_checksum_ok(gstin: str) -> bool:
    if len(gstin) != 15 or any(c not in GSTIN_CHARSET for c in gstin):
        return False
    return gstin_check_char(gstin[:14]) == gstin[14]


def make_gstin(state_code: str, pan: str, entity_no: str = "1") -> str:
    """Build a structurally valid GSTIN from a PAN (used for fictional sample data)."""
    body = f"{state_code}{pan}{entity_no}Z"
    return body + gstin_check_char(body)


def gstin_pan(gstin: str) -> str:
    return gstin[2:12]


# Checksum algorithms that rules are allowed to reference by name.
CHECKSUMS = {
    "gstin_mod36": gstin_checksum_ok,
}


def normalize_tax_id(value: str) -> str:
    return regex.sub(r"[\s]", "", (value or "").upper())


def safe_fullmatch(pattern: str, value: str) -> bool:
    """Full match with a hard timeout. A timeout counts as no match."""
    try:
        return regex.fullmatch(pattern, value, timeout=REGEX_TIMEOUT_SECONDS) is not None
    except (TimeoutError, regex.error):
        return False


@dataclass
class TaxRule:
    id: int
    country: str
    tax_type: str
    regex: str
    status: str               # seed | verified | provisional | disabled
    checksum_algo: str | None = None
    example: str | None = None
    description: str = ""
    source_url: str | None = None

    @classmethod
    def from_row(cls, row) -> "TaxRule":
        return cls(id=row["id"], country=row["country"], tax_type=row["tax_type"], regex=row["regex"], status=row["status"],
                   checksum_algo=row["checksum_algo"], example=row["example"], description=row["description"], source_url=row["source_url"])

    def matches(self, value: str) -> bool:
        return safe_fullmatch(self.regex, value)

    def checksum_ok(self, value: str) -> bool | None:
        """None when the rule has no (known) checksum."""
        fn = CHECKSUMS.get(self.checksum_algo or "")
        return None if fn is None else fn(value)

    def valid(self, value: str) -> bool:
        return self.matches(value) and self.checksum_ok(value) is not False


def _one_substitution_away(value: str, rule: TaxRule) -> str | None:
    """A single-character substitution that makes the value fully valid, if one exists (typo detection)."""
    for i in range(len(value)):
        for ch in ALNUM:
            if ch == value[i]:
                continue
            candidate = value[:i] + ch + value[i + 1:]
            if rule.valid(candidate):
                return candidate
    return None


def interpret(value: str, rule: TaxRule, other_rules: list[TaxRule]) -> CheckOutcome:
    """Turn a tax ID + the stated country's rule into a check outcome (brief §10)."""
    v = normalize_tax_id(value)
    ev = {"tax_id": v, "country": rule.country, "tax_type": rule.tax_type, "rule_id": rule.id,
          "rule_status": rule.status, "rule_regex": rule.regex, "checksum_algo": rule.checksum_algo}
    if rule.matches(v):
        cs = rule.checksum_ok(v)
        if cs is False:
            return failed("TAXID_CHECKSUM_FAIL", f"{rule.tax_type} '{v}' has the right format but its check character is wrong, "
                          "which usually means a typo.", **ev)
        note = " Format and checksum are valid." if cs else " Format is valid."
        return passed(f"{rule.tax_type} '{v}' matches the {rule.country} {rule.status} rule.{note} "
                      "Note: a valid format does not prove the number is registered (production would query the official registry).", **ev)

    for other in other_rules:
        if other.country != rule.country and other.status != "disabled" and other.valid(v):
            ev["matched_other_rule"] = {"country": other.country, "tax_type": other.tax_type, "rule_id": other.id}
            return failed("TAXID_WRONG_COUNTRY_FORMAT", f"'{v}' does not fit the declared country ({rule.country}) but is a valid "
                          f"{other.country} {other.tax_type} format. That mismatch needs a person's review.", **ev)

    if rule.status == "provisional":
        return failed("RULE_PROVISIONAL_MISMATCH", f"'{v}' does not match the provisional {rule.country} {rule.tax_type} rule learned from "
                      "the web. Either the rule or the ID is wrong, so a person decides.", **ev)

    fix = _one_substitution_away(v, rule)
    if fix is not None:
        ev["one_character_from_valid"] = True
        return failed("TAXID_CHECKSUM_FAIL", f"{rule.tax_type} '{v}' is one character away from a valid number, which looks like a typo.", **ev)
    return failed("TAXID_FORMAT_FAIL", f"'{v}' does not match the {rule.country} {rule.tax_type} format ({rule.description or rule.regex}).", **ev)


def pan_entity_outcome(pan: str, entity_type: str) -> CheckOutcome:
    """PAN 4th character must agree with the declared entity type."""
    code = pan[3] if len(pan) >= 4 else "?"
    holder = PAN_ENTITY_CODES.get(code, "unknown")
    allowed = PAN_CODES_FOR_ENTITY.get(entity_type, set())
    ev = {"pan": pan, "fourth_char": code, "pan_holder_type": holder, "entity_type": entity_type}
    if code in allowed:
        return passed(f"PAN 4th character '{code}' ({holder}) is consistent with a {entity_type.replace('_', ' ')}.", **ev)
    return failed("ENTITY_TYPE_UNCLEAR", f"PAN 4th character '{code}' means '{holder.replace('_', ' ')}', which contradicts the declared "
                  f"entity type '{entity_type.replace('_', ' ')}'.", **ev)


def gstin_pan_outcome(gstin: str, pan: str) -> CheckOutcome:
    embedded = gstin_pan(gstin)
    ev = {"gstin": gstin, "pan": pan, "pan_in_gstin": embedded}
    if embedded == pan:
        return passed(f"The PAN embedded in the GSTIN ({embedded}) matches the PAN on the documents.", **ev)
    return failed("TAXID_INCONSISTENT", f"The GSTIN contains PAN {embedded}, but the documents show PAN {pan}. These should be identical.", **ev)
