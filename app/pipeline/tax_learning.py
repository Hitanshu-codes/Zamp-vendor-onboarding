"""Learning a tax-ID format rule on a registry miss. Web search is wired in P6; until then this
only honours the negative cache and reports that no rule is available."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app import clock, db
from app.domain.taxids import TaxRule


@dataclass
class LearnResult:
    rule: TaxRule | None
    summary: str
    evidence: dict = field(default_factory=dict)


def recent_miss(country: str, tax_type: str) -> bool:
    """Negative cache: the same miss within 24h is not searched again."""
    since = (clock.now() - timedelta(hours=24)).isoformat(timespec="seconds")
    row = db.query_one("SELECT 1 FROM tax_rule_search_log WHERE country = ? AND tax_type = ? AND outcome != 'found' AND ts >= ?",
                       (country, tax_type, since))
    return row is not None


async def learn(ctx, country: str, tax_type: str) -> LearnResult:
    if recent_miss(country, tax_type):
        return LearnResult(None, "A search for this rule failed in the last 24 hours, so it was not repeated (negative cache).",
                           {"negative_cache": True})
    return LearnResult(None, "Web search for new rules is not enabled in this build.", {"search": "disabled"})
