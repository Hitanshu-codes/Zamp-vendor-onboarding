"""Tax ID validators implemented in code. Learned (web) rules may only reference
checksum algorithms named in CHECKSUMS; no code from the web is ever executed."""
from __future__ import annotations

import regex

GSTIN_CHARSET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
GSTIN_RE = regex.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
PAN_RE = regex.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")

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


# Checksum algorithms that learned rules are allowed to reference by name.
CHECKSUMS = {
    "gstin_mod36": gstin_checksum_ok,
}


def normalize_tax_id(value: str) -> str:
    return regex.sub(r"[\s]", "", (value or "").upper())
