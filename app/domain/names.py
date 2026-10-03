"""Name normalisation. Matching logic is added in P2."""
from __future__ import annotations

import unicodedata

import regex

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
}


def strip_diacritics(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def normalize_name(name: str) -> str:
    text = strip_diacritics(name or "").lower()
    text = text.replace("&", " and ")
    text = regex.sub(r"[^\p{L}\p{N}\s]", " ", text)
    tokens = [SUFFIX_CANON.get(t, t) for t in text.split()]
    return " ".join(tokens)
