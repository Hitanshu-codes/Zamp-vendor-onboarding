"""Name normalisation and matching.

Rule of thumb: only *identical* normalised names pass on code alone. Fuzzy
similarity never passes by itself; it only decides whether a case is a clear
mismatch (vendor can fix) or ambiguous (LLM adjudicates, a human decides on
any doubt).
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from typing import Literal

import regex
from rapidfuzz import fuzz

from app.models import CheckOutcome, failed, passed

# Legal-form canonicalisation applied token by token after punctuation is stripped.
SUFFIX_CANON = {
    "pvt": "private",
    "pte": "private",
    "ltd": "limited",
    "ltda": "limitada",
    "corp": "corporation",
    "co": "company",
    "inc": "incorporated",
    "intl": "international",
    "mfg": "manufacturing",
    "bros": "brothers",
    "svcs": "services",
    "plc": "public limited company",
}

# Tokens that only express legal form. A difference made of these alone is benign.
LEGAL_FORM_TOKENS = {
    "private", "limited", "public", "company", "corporation", "incorporated", "llp", "llc", "gmbh", "ag",
    "sa", "sas", "sl", "bv", "nv", "pty", "limitada", "srl", "spa", "oy", "ab", "as", "kg", "the",
}

# Known transliteration variants, treated as the same token.
TRANSLITERATION_GROUPS = [
    {"mohammed", "muhammad", "mohammad", "mohamed", "muhammed"},
    {"muller", "mueller"},
    {"schroder", "schroeder"},
    {"jose", "josé"},
    {"sri", "shri", "shree"},
    {"aggarwal", "agarwal", "agrawal"},
]
_TRANSLIT = {tok: min(group) for group in TRANSLITERATION_GROUPS for tok in group}

TRADE_AS = regex.compile(r"\b(t/a|trading as|d/b/a|dba|doing business as)\b", regex.IGNORECASE)


def strip_diacritics(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def normalize_name(name: str) -> str:
    text = strip_diacritics(name or "").lower()
    text = text.replace("&", " and ")
    # Dotted acronyms collapse to one token: "S.A.S." -> "sas", "F.Z.E." -> "fze", "B.V." -> "bv".
    text = regex.sub(r"\b((?:\p{L}\.){2,})", lambda m: m.group(1).replace(".", "") + " ", text)
    text = regex.sub(r"[^\p{L}\p{N}\s]", " ", text)
    tokens: list[str] = []
    for t in text.split():
        tokens.extend(SUFFIX_CANON.get(t, t).split())
    return " ".join(tokens)


def scripts_used(name: str) -> set[str]:
    """Unicode scripts of the letters in a name (Latin, Cyrillic, Greek, ...)."""
    found: set[str] = set()
    for ch in name or "":
        if not ch.isalpha():
            continue
        for script in ("Latin", "Cyrillic", "Greek", "Arabic", "Devanagari", "Han", "Hebrew", "Thai"):
            if regex.match(rf"\p{{Script={script}}}", ch):
                found.add(script)
                break
        else:
            found.add("Other")
    return found


def is_mixed_script(name: str) -> bool:
    """True when a single word mixes scripts, e.g. a Cyrillic 'а' inside a Latin word (homoglyph trick)."""
    return any(len(scripts_used(word)) > 1 for word in (name or "").split())


@dataclass
class NameMatch:
    a: str
    b: str
    normalized_a: str
    normalized_b: str
    exact: bool
    fuzzy_score: float
    distinct_tokens_a: list[str] = field(default_factory=list)
    distinct_tokens_b: list[str] = field(default_factory=list)
    mixed_script: bool = False
    trade_name_pattern: bool = False

    @property
    def only_benign_differences(self) -> bool:
        """Distinct tokens are only legal-form words or known transliteration variants."""
        da = {_TRANSLIT.get(t, t) for t in self.distinct_tokens_a} - LEGAL_FORM_TOKENS
        db = {_TRANSLIT.get(t, t) for t in self.distinct_tokens_b} - LEGAL_FORM_TOKENS
        return da == db

    def evidence(self) -> dict:
        return {
            "name_a": self.a, "name_b": self.b,
            "normalized_a": self.normalized_a, "normalized_b": self.normalized_b,
            "exact": self.exact, "fuzzy_score": round(self.fuzzy_score, 1),
            "distinct_tokens_a": self.distinct_tokens_a, "distinct_tokens_b": self.distinct_tokens_b,
            "mixed_script": self.mixed_script,
        }


def match_names(a: str, b: str) -> NameMatch:
    na, nb = normalize_name(a), normalize_name(b)
    ta, tb = na.split(), nb.split()
    return NameMatch(
        a=a, b=b, normalized_a=na, normalized_b=nb,
        exact=bool(na) and na == nb,
        fuzzy_score=float(fuzz.token_sort_ratio(na, nb)) if na and nb else 0.0,
        distinct_tokens_a=[t for t in ta if t not in tb],
        distinct_tokens_b=[t for t in tb if t not in ta],
        mixed_script=is_mixed_script(a) or is_mixed_script(b),
        trade_name_pattern=bool(TRADE_AS.search(a or "") or TRADE_AS.search(b or "")),
    )


Band = Literal["exact", "clear_mismatch", "ambiguous"]


def band(m: NameMatch, low: int) -> Band:
    """Where a pair falls before any LLM is consulted."""
    if m.mixed_script:
        return "ambiguous"
    if m.exact:
        return "exact"
    if m.trade_name_pattern:
        return "ambiguous"
    if m.fuzzy_score < low and not m.only_benign_differences:
        return "clear_mismatch"
    return "ambiguous"


LLMVerdict = Literal["yes", "no", "unsure"]


def name_outcome(m: NameMatch, low: int, llm_verdict: LLMVerdict | None, llm_available: bool, label: str) -> CheckOutcome:
    """Combine the code-side band with the LLM's verdict (only consulted in the ambiguous band).

    `label` describes the pair for explanations, e.g. "form legal name vs bank account holder".
    """
    ev = m.evidence()
    b = band(m, low)
    if m.mixed_script:
        return failed("NAME_AMBIGUOUS", f"{label}: the name mixes alphabets (e.g. a Cyrillic letter inside a Latin word). "
                      "This is a known impersonation trick, so a person must check it.", **ev, note="mixed scripts")
    if b == "exact":
        return passed(f"{label}: identical after normalising case, punctuation and legal suffixes.", **ev)
    if b == "clear_mismatch":
        return failed("NAME_MISMATCH_CLEAR", f"{label}: '{m.a}' and '{m.b}' clearly differ (similarity {m.fuzzy_score:.0f}%).", **ev)

    ev["llm_consulted"] = llm_available
    ev["llm_verdict"] = llm_verdict
    if not llm_available or llm_verdict is None:
        return failed("NAME_AMBIGUOUS", f"{label}: names are similar ({m.fuzzy_score:.0f}%) but not identical, and the LLM "
                      "could not be consulted, so a person decides.", **ev)
    if llm_verdict == "yes" and m.only_benign_differences:
        return passed(f"{label}: differ only in legal form or spelling variants ({', '.join(m.distinct_tokens_a + m.distinct_tokens_b) or 'none'}); "
                      "the LLM independently judged them the same entity. Note: passed on agreement, not on identity.", **ev, warn_note=True)
    if llm_verdict == "yes":
        return failed("NAME_LLM_DISAGREE", f"{label}: the LLM thinks these are the same entity, but the names differ in meaningful words "
                      f"({', '.join(m.distinct_tokens_a + m.distinct_tokens_b)}). Code and LLM disagree, so a person decides.", **ev)
    if llm_verdict == "no" and m.fuzzy_score >= low:
        return failed("NAME_AMBIGUOUS", f"{label}: names look similar ({m.fuzzy_score:.0f}%) but the LLM judged them different entities. "
                      "A person decides.", **ev)
    return failed("NAME_AMBIGUOUS", f"{label}: names are close but the LLM was unsure whether they are the same entity.", **ev)
