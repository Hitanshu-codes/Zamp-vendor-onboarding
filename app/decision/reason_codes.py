"""Reason code catalogue. Severity comes from here, so a code can never be emitted with the wrong weight."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Severity = Literal["info", "soft", "fixable", "escalate", "reject"]


@dataclass(frozen=True)
class ReasonCode:
    code: str
    severity: Severity
    meaning: str           # internal, plain English
    risk_points: int = 0   # only for soft codes
    suspicious: bool = False  # True: vendor is never told the reason (no tipping off)


_CODES = [
    # fixable: communicate back to the vendor
    ReasonCode("FORM_FIELD_MISSING", "fixable", "A required form field is empty."),
    ReasonCode("DOC_MISSING", "fixable", "A required document was not provided."),
    ReasonCode("DOC_WRONG_TYPE", "fixable", "The document's content does not match the slot it was uploaded to."),
    ReasonCode("DOC_EXPIRED", "fixable", "The document's validity date has passed."),
    ReasonCode("DOC_STALE", "fixable", "The bank letter is older than the allowed age."),
    ReasonCode("DOC_UNREADABLE", "fixable", "The file is corrupt, encrypted, or too poor to read."),
    ReasonCode("TAXID_CHECKSUM_FAIL", "fixable", "Tax ID has the right shape but fails its checksum, which suggests a typo."),
    ReasonCode("TAXID_FORMAT_FAIL", "fixable", "Tax ID does not match the country's verified format."),
    ReasonCode("NAME_MISMATCH_CLEAR", "fixable", "Names across the form and documents clearly differ."),
    ReasonCode("BANK_DETAILS_INVALID", "fixable", "Account number or bank code fails its format or checksum (likely a typo)."),
    ReasonCode("BANK_HOLDER_NOT_COMPANY", "fixable", "The bank account appears to belong to an individual, not the company."),
    # escalate: manual review
    ReasonCode("TAXID_WRONG_COUNTRY_FORMAT", "escalate", "Tax ID is a valid format for a different country than declared.", suspicious=True),
    ReasonCode("RULE_PROVISIONAL_MISMATCH", "escalate", "Tax ID fails a provisional (web-learned) rule: either the rule or the ID is wrong."),
    ReasonCode("TAX_RULE_UNAVAILABLE", "escalate", "No rule exists or could be learned for this country and tax type."),
    ReasonCode("NAME_AMBIGUOUS", "escalate", "Names are similar but not identical; a person should judge."),
    ReasonCode("NAME_LLM_DISAGREE", "escalate", "The similarity score and the LLM's judgement disagree."),
    ReasonCode("BANK_COLLISION", "escalate", "The bank account already belongs to a different vendor.", suspicious=True),
    ReasonCode("SANCTIONS_FUZZY", "escalate", "Near match on the sanctions list.", suspicious=True),
    ReasonCode("WATCHLIST_HIGH", "escalate", "Match on a high-severity internal watchlist entry (or a near match on a blocked one).", suspicious=True),
    ReasonCode("INJECTION_DETECTED", "escalate", "Instruction-like text was found in vendor-supplied content.", suspicious=True),
    ReasonCode("LLM_UNAVAILABLE", "escalate", "The LLM failed, timed out, or returned invalid output."),
    ReasonCode("BANK_COUNTRY_MISMATCH", "escalate", "The bank's country differs from the vendor's country.", suspicious=True),
    ReasonCode("DATES_IMPOSSIBLE", "escalate", "Document dates are inconsistent (e.g. tax registration before incorporation).", suspicious=True),
    ReasonCode("UNREQUESTED_CHANGE", "escalate", "A resubmission changed details we did not ask to change.", suspicious=True),
    ReasonCode("BANK_CHANGE_REQUEST", "escalate", "An approved vendor asked to change bank details.", suspicious=True),
    ReasonCode("ENTITY_TYPE_UNCLEAR", "escalate", "Signals contradict the declared entity type."),
    ReasonCode("TAXID_INCONSISTENT", "escalate", "Tax identifiers on the form and documents contradict each other (e.g. GSTIN vs PAN).", suspicious=True),
    ReasonCode("CHECK_MISSING", "escalate", "A required check did not produce a passing result; a missing check is never treated as a pass."),
    # reject
    ReasonCode("SANCTIONS_EXACT", "reject", "Exact match on the sanctions list.", suspicious=True),
    ReasonCode("WATCHLIST_BLOCKED", "reject", "Exact match on a blocked internal watchlist entry.", suspicious=True),
    ReasonCode("DUPLICATE_VENDOR", "reject", "A vendor with this tax ID is already active.", suspicious=True),
    # soft: risk points only
    ReasonCode("FREE_EMAIL_DOMAIN", "soft", "Company vendor uses a free email provider.", risk_points=2),
    ReasonCode("WATCHLIST_WATCH", "soft", "Match on a low-severity watchlist entry.", risk_points=2, suspicious=True),
    ReasonCode("DEGRADED_DOCUMENT", "soft", "Document is readable but low quality.", risk_points=1),
]

CATALOGUE: dict[str, ReasonCode] = {c.code: c for c in _CODES}

SEVERITY_RANK: dict[str, int] = {"info": 0, "soft": 1, "fixable": 2, "escalate": 3, "reject": 4}


def get(code: str) -> ReasonCode:
    return CATALOGUE[code]
