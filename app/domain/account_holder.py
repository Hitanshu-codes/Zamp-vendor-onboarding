"""Is the bank account holder a company or an individual?

Layered, cheapest first: structural signals in the name and account type (code),
then cross-references to the legal name and directors (code), then the LLM, but
only when the code says `unclear`, and only to move it toward a label.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import regex

from app.domain.names import LEGAL_FORM_TOKENS, normalize_name
from app.models import CheckOutcome, failed, passed

Label = Literal["company", "individual", "unclear"]

# "as"/"ab"/"sa" are legal forms in some countries but also ordinary words, so they are not suffix signals here.
COMPANY_SUFFIXES = (LEGAL_FORM_TOKENS - {"the", "company", "as", "ab", "sa", "public"}) | {"plc", "pte", "kk", "sarl", "sdn", "bhd"}
BUSINESS_WORDS = {
    "enterprises", "enterprise", "traders", "trading", "industries", "industrial", "solutions", "services", "associates",
    "brothers", "sons", "exports", "imports", "logistics", "technologies", "technology", "systems", "consulting",
    "consultants", "group", "holdings", "agencies", "agency", "stores", "works", "international", "manufacturing",
    "textiles", "foods", "pharma", "motors", "electricals", "construction", "builders", "supplies", "distributors",
    "partners", "ventures", "labs", "studio", "company", "corporation", "incorporated",
}
TITLES = {"mr", "mrs", "ms", "miss", "shri", "smt", "sri", "kumari", "dr", "prof", "mx"}
RELATION = regex.compile(r"\b[sdw]\s*/\s*o\b|\bson of\b|\bdaughter of\b|\bwife of\b", regex.IGNORECASE)
JOINT = regex.compile(r"\s(?:and|&|/)\s|\s/|/\s", regex.IGNORECASE)


@dataclass
class HolderAssessment:
    holder: str
    label: Label
    signals: list[str] = field(default_factory=list)
    matches_company: bool = False
    matches_director: str | None = None
    llm_consulted: bool = False
    llm_label: str | None = None

    def evidence(self) -> dict:
        return {
            "holder": self.holder, "label": self.label, "signals": self.signals,
            "matches_company_name": self.matches_company, "matches_director": self.matches_director,
            "llm_consulted": self.llm_consulted, "llm_label": self.llm_label,
        }


def assess_holder(holder: str, legal_name: str, directors: list[str], account_type: str = "unknown") -> HolderAssessment:
    """Layers 1 and 2: structural signals and cross-references, no LLM."""
    norm = normalize_name(holder)
    tokens = norm.split()
    raw_tokens = regex.sub(r"[^\p{L}\p{N}\s]", " ", (holder or "").lower()).split()
    signals: list[str] = []
    strong_company = strong_individual = False
    medium_company = medium_individual = 0

    if set(tokens) & COMPANY_SUFFIXES or set(raw_tokens) & {"pvt", "ltd", "llp", "llc", "inc", "gmbh"}:
        signals.append("company_suffix")
        strong_company = True
    business = set(tokens) & BUSINESS_WORDS
    if business:
        signals.append("business_word")
        medium_company += 1
    if raw_tokens and raw_tokens[0] in TITLES:
        signals.append("personal_title")
        strong_individual = True
    if RELATION.search(holder or ""):
        signals.append("relation_marker")
        strong_individual = True
    if JOINT.search(holder or "") and not strong_company and not business:
        signals.append("joint_holders")
        strong_individual = True
    if 2 <= len(tokens) <= 3 and not business and not strong_company and all(t.isalpha() for t in tokens):
        signals.append("person_like_name")
        medium_individual += 1

    if account_type == "current":
        signals.append("current_account")
        medium_company += 1
    elif account_type == "savings":
        signals.append("savings_account")
        medium_individual += 1

    matches_company = bool(norm) and norm == normalize_name(legal_name)
    if matches_company:
        signals.append("matches_company_name")
        strong_company = True

    matched_director = None
    for d in directors or []:
        if norm and norm == normalize_name(d):
            matched_director = d
            signals.append("matches_director")
            medium_individual += 1
            break

    if strong_company and not strong_individual:
        label: Label = "company"
    elif strong_individual and not strong_company:
        label = "individual"
    elif strong_company and strong_individual:
        label = "unclear"
    elif medium_company >= 2 and medium_individual == 0:
        label = "company"
    elif medium_individual >= 1 and medium_company == 0:
        label = "individual"
    else:
        label = "unclear"
    return HolderAssessment(holder=holder, label=label, signals=signals, matches_company=matches_company, matches_director=matched_director)


def apply_llm(assessment: HolderAssessment, llm_label: str | None) -> HolderAssessment:
    """Layer 3. The LLM may only move `unclear` to a label; it can never override a code-derived label."""
    assessment.llm_consulted = True
    assessment.llm_label = llm_label
    if assessment.label == "unclear" and llm_label in ("company", "individual"):
        assessment.label = llm_label  # type: ignore[assignment]
        assessment.signals.append(f"llm_suggested_{llm_label}")
    return assessment


def holder_outcome(a: HolderAssessment, entity_type: str, owners: list[str], other_anomalies: list[str]) -> CheckOutcome:
    ev = a.evidence() | {"entity_type": entity_type, "other_anomalies": other_anomalies}
    sig = ", ".join(a.signals) or "none"

    if entity_type == "sole_proprietor":
        owner_match = a.matches_company or any(normalize_name(a.holder) == normalize_name(o) for o in owners or [])
        if owner_match:
            return passed(f"Sole proprietor: account holder '{a.holder}' matches the proprietor or trade name, so a personal account is acceptable.", **ev)
        if a.label == "company":
            return passed(f"Account holder '{a.holder}' looks like a business account ({sig}).", **ev)
        return failed("ENTITY_TYPE_UNCLEAR", f"Sole proprietor, but the account holder '{a.holder}' does not match the proprietor named on the documents. "
                      "A person should confirm who owns this account.", **ev)

    if a.label == "company":
        return passed(f"Account holder '{a.holder}' is a company account (signals: {sig}).", **ev)
    if a.label == "individual" and not other_anomalies:
        who = f", and the name matches director {a.matches_director}" if a.matches_director else ""
        return failed("BANK_HOLDER_NOT_COMPANY", f"Account holder '{a.holder}' looks like a personal account (signals: {sig}){who}. "
                      "Payments to a company should go to an account in the company's name.", **ev)
    if a.label == "individual":
        return failed("ENTITY_TYPE_UNCLEAR", f"Account holder '{a.holder}' looks personal, and there are other anomalies "
                      f"({', '.join(other_anomalies)}). Together these need a person's review.", **ev)
    return failed("ENTITY_TYPE_UNCLEAR", f"Could not tell whether '{a.holder}' is a company or a person (signals: {sig}). A person should check.", **ev)
