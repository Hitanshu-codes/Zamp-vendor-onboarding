"""Sanctions screening against the mock list (MOCK-LIST-A, fictional entities)."""
from __future__ import annotations

from dataclasses import dataclass

from rapidfuzz import fuzz

from app import db
from app.domain.names import normalize_name
from app.models import CheckOutcome, failed, passed


@dataclass
class SanctionsEntry:
    id: int
    name: str
    aliases: list[str]
    source_label: str


@dataclass
class SanctionsHit:
    entry: SanctionsEntry
    screened_role: str
    screened_name: str
    matched_name: str
    match_type: str  # exact | fuzzy
    score: float


def load_entries() -> list[SanctionsEntry]:
    return [SanctionsEntry(r["id"], r["name"], db.loads(r["aliases_json"], []), r["source_label"])
            for r in db.query("SELECT * FROM sanctions_list ORDER BY id")]


def screen(names: list[tuple[str, str]], entries: list[SanctionsEntry], threshold: int) -> list[SanctionsHit]:
    """names: (role, name) pairs, e.g. ("legal name", "Acme Ltd"). Returns the best hit per (role, entry)."""
    hits: list[SanctionsHit] = []
    for role, name in names:
        n = normalize_name(name)
        if not n:
            continue
        for entry in entries:
            best: SanctionsHit | None = None
            for candidate in [entry.name, *entry.aliases]:
                c = normalize_name(candidate)
                if n == c:
                    best = SanctionsHit(entry, role, name, candidate, "exact", 100.0)
                    break
                score = float(fuzz.token_sort_ratio(n, c))
                if score >= threshold and (best is None or score > best.score):
                    best = SanctionsHit(entry, role, name, candidate, "fuzzy", score)
            if best:
                hits.append(best)
    return hits


def sanctions_outcome(hits: list[SanctionsHit]) -> CheckOutcome:
    def ev(h: SanctionsHit) -> dict:
        return {"list": h.entry.source_label, "entry_id": h.entry.id, "entry_name": h.entry.name, "matched_name": h.matched_name,
                "screened_role": h.screened_role, "screened_name": h.screened_name, "match_type": h.match_type, "score": round(h.score, 1)}

    exact = [h for h in hits if h.match_type == "exact"]
    if exact:
        h = exact[0]
        return failed("SANCTIONS_EXACT", f"The {h.screened_role} '{h.screened_name}' exactly matches '{h.matched_name}' on {h.entry.source_label}.",
                      hits=[ev(x) for x in hits])
    if hits:
        h = max(hits, key=lambda x: x.score)
        return failed("SANCTIONS_FUZZY", f"The {h.screened_role} '{h.screened_name}' is a near match ({h.score:.0f}%) to '{h.matched_name}' on "
                      f"{h.entry.source_label}. Near matches are never auto-rejected; a compliance reviewer compares address and registration details.",
                      hits=[ev(x) for x in hits])
    return passed("No exact or near matches on MOCK-LIST-A for the legal name, trade name, directors or account holder.")
