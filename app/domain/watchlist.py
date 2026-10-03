"""Internal watchlist screening on every identifier, not just names.

Strong identifiers (bank account, tax ID, email domain, phone) match exactly
after normalisation. Names and directors also match fuzzily, but a fuzzy hit
can only ever escalate, never reject.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from rapidfuzz import fuzz

from app import db
from app.models import CheckOutcome, failed, passed, warned
from app.domain.identifiers import normalize_identifier

STRONG_KINDS = {"bank_account", "tax_id", "email_domain", "phone"}
KIND_LABEL = {"bank_account": "bank account", "tax_id": "tax ID", "email_domain": "email domain", "phone": "phone number",
              "address": "address", "director": "director", "name": "name"}


@dataclass
class WatchEntry:
    id: int
    label: str
    severity: str
    reason: str
    identifiers: list[tuple[str, str, str]] = field(default_factory=list)  # (kind, normalized, display)


@dataclass
class WatchHit:
    entry: WatchEntry
    kind: str
    match_type: str
    score: float
    candidate_display: str
    entry_display: str


def load_entries(today: date) -> list[WatchEntry]:
    rows = db.query("SELECT * FROM watchlist WHERE active = 1")
    idents = db.query("SELECT * FROM watchlist_identifiers")
    by_id: dict[int, list] = {}
    for i in idents:
        by_id.setdefault(i["watchlist_id"], []).append((i["kind"], i["value_normalized"], i["value_display"]))
    out = []
    for r in rows:
        if r["expires_at"] and r["expires_at"][:10] < today.isoformat():
            continue
        out.append(WatchEntry(r["id"], r["label"], r["severity"], r["reason"], by_id.get(r["id"], [])))
    return out


def screen(candidate: dict[str, list[str]], entries: list[WatchEntry], fuzzy_threshold: int) -> list[WatchHit]:
    """candidate: kind -> list of raw values from the submission (form + documents)."""
    hits: list[WatchHit] = []
    for entry in entries:
        for kind, norm_value, display in entry.identifiers:
            for raw in candidate.get(kind, []):
                c = normalize_identifier(kind, raw)
                if not c:
                    continue
                if c == norm_value:
                    hits.append(WatchHit(entry, kind, "exact", 100.0, raw, display))
                elif kind in ("name", "director"):
                    score = float(fuzz.token_sort_ratio(c, norm_value))
                    if score >= fuzzy_threshold:
                        hits.append(WatchHit(entry, kind, "fuzzy", score, raw, display))
    return hits


def watchlist_outcome(hits: list[WatchHit], candidate_name: str) -> CheckOutcome:
    def ev(h: WatchHit) -> dict:
        return {"entry_id": h.entry.id, "entry_label": h.entry.label, "severity": h.entry.severity, "matched_kind": h.kind,
                "match_type": h.match_type, "score": round(h.score, 1), "submitted_value": h.candidate_display,
                "entry_value": h.entry_display, "entry_reason": h.entry.reason}

    if not hits:
        return passed("No matches against the internal watchlist on name, bank account, tax ID, email domain, phone, address or directors.")
    evidence = [ev(h) for h in hits]

    def describe(h: WatchHit) -> str:
        name_hit = any(x.entry.id == h.entry.id and x.kind == "name" for x in hits)
        if h.kind != "name" and not name_hit:
            return (f"The company name '{candidate_name}' is new, but the {KIND_LABEL[h.kind]} matches {h.entry.severity} "
                    f"watchlist entry #{h.entry.id} ('{h.entry.label}'). Identifiers like this are hard to change, so the link matters.")
        return f"The {KIND_LABEL[h.kind]} {'matches' if h.match_type == 'exact' else f'is a near match ({h.score:.0f}%) to'} " \
               f"{h.entry.severity} watchlist entry #{h.entry.id} ('{h.entry.label}')."

    blocked_exact = [h for h in hits if h.entry.severity == "blocked" and h.match_type == "exact"]
    if blocked_exact:
        h = next((x for x in blocked_exact if x.kind in STRONG_KINDS), blocked_exact[0])
        return failed("WATCHLIST_BLOCKED", describe(h), hits=evidence)
    escalating = [h for h in hits if h.entry.severity == "high" or (h.entry.severity == "blocked" and h.match_type == "fuzzy")]
    if escalating:
        h = escalating[0]
        extra = " Fuzzy matches are escalated, never auto-rejected." if h.match_type == "fuzzy" else ""
        return failed("WATCHLIST_HIGH", describe(h) + extra + f" Prior case: {h.entry.reason}", hits=evidence)
    h = hits[0]
    return warned(describe(h) + " Low-severity entry: adds risk points only.", code="WATCHLIST_WATCH", hits=evidence)
