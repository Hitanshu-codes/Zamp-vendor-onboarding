"""Stage 5: Find the format rule for (country, tax type). Learns it from the web on a miss (see tax_learning)."""
from __future__ import annotations

from datetime import date

from app import clock, db
from app.config import config
from app.domain.taxids import PRIMARY_TAX_TYPE, TaxRule
from app.models import failed, passed
from app.pipeline import tax_learning
from app.pipeline.context import RunContext

STATUS_ORDER = {"verified": 0, "seed": 1, "provisional": 2}


def find_rule(country: str, tax_type: str) -> TaxRule | None:
    rows = db.query("SELECT * FROM tax_rules WHERE country = ? AND tax_type = ? AND status != 'disabled'", (country, tax_type))
    if not rows:
        return None
    best = min(rows, key=lambda r: (STATUS_ORDER.get(r["status"], 9), -r["id"]))
    return TaxRule.from_row(best)


def all_active_rules() -> list[TaxRule]:
    return [TaxRule.from_row(r) for r in db.query("SELECT * FROM tax_rules WHERE status != 'disabled'")]


def reverify_due(rule_row) -> bool:
    last = (rule_row["last_verified_at"] or rule_row["created_at"])[:10]
    return (clock.today() - date.fromisoformat(last)).days > config.rule_reverify_days


async def run(ctx: RunContext) -> None:
    country = ctx.form.get("country", "")
    ctx.tax_type = ctx.form.get("tax_id_type") or PRIMARY_TAX_TYPE.get(country, "TAX_ID")
    ctx.tax_rule = find_rule(country, ctx.tax_type)  # a fact, set even when the check result is reused

    async def outcome():
        rule = ctx.tax_rule
        if rule is not None:
            db.execute("UPDATE tax_rules SET times_used = times_used + 1 WHERE id = ?", (rule.id,))
            row = db.query_one("SELECT * FROM tax_rules WHERE id = ?", (rule.id,))
            note = " Re-verification is due." if reverify_due(row) else ""
            return passed(f"Registry hit: {country} {ctx.tax_type} rule ({rule.status}), no web search needed.{note}",
                          cache="hit", rule_id=rule.id, rule_status=rule.status, regex=rule.regex, checksum=rule.checksum_algo,
                          source_url=rule.source_url, times_used=row["times_used"], reverify_due=bool(note))
        learned = await tax_learning.learn(ctx, country, ctx.tax_type)
        if learned.rule is not None:
            ctx.tax_rule = learned.rule
            return passed(f"Registry miss. {learned.summary}", cache="miss_learned", rule_id=learned.rule.id, rule_status="provisional",
                          regex=learned.rule.regex, source_url=learned.rule.source_url, **learned.evidence)
        return failed("TAX_RULE_UNAVAILABLE", f"No format rule for {country} {ctx.tax_type}. {learned.summary} "
                      "The tax ID cannot be format-checked, so a person decides.", cache="miss", **learned.evidence)

    await ctx.check("tax_rule", outcome)
