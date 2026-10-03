"""Stage 8: Risk screening: sanctions, internal watchlist, duplicate vendor, bank collision."""
from __future__ import annotations

from app import clock, db
from app.config import config
from app.domain import sanctions, vendor_master, watchlist
from app.domain.validators import email_domain
from app.pipeline import forms
from app.pipeline.context import RunContext


def _collect(ctx: RunContext) -> dict[str, list[str]]:
    f = ctx.form
    names = [f.get("legal_name", ""), f.get("trade_name", "")]
    accounts = [forms.account(f)]
    tax_ids = [f.get("tax_id", "")]
    addresses = [f.get("registered_address", "")]
    directors: list[str] = []
    holders = [f.get("bank_account_holder", "")]
    for slot in ("tax_cert", "incorporation_cert", "bank_letter"):
        ex, _ = ctx.usable(slot)
        if not ex:
            continue
        names += [ex.legal_name or "", ex.trade_name or ""]
        accounts += [ex.account_number or "", ex.iban or ""]
        tax_ids += [ex.tax_id or "", ex.secondary_tax_id or ""]
        addresses.append(ex.address or "")
        directors += ex.directors_or_owners
        holders.append(ex.account_holder or "")
    uniq = lambda xs: list(dict.fromkeys(x for x in xs if x))  # noqa: E731
    return {"name": uniq(names), "bank_account": uniq(accounts), "tax_id": uniq(tax_ids), "address": uniq(addresses),
            "director": uniq(directors), "holder": uniq(holders), "email_domain": uniq([email_domain(f.get("contact_email", ""))]),
            "phone": uniq([f.get("contact_phone", "")])}


def cleared_entries(submission_id: int, kind: str) -> dict[int, dict]:
    """Hits a reviewer already cleared for this submission (with a reason) are not raised again on re-runs."""
    out: dict[int, dict] = {}
    rows = db.query("SELECT a.* FROM review_actions a JOIN review_cases c ON c.id = a.case_id WHERE c.submission_id = ? AND a.action = 'clear_hit'",
                    (submission_id,))
    for r in rows:
        p = db.loads(r["payload_json"], {})
        if p.get("list") == kind and p.get("entry_id"):
            out[int(p["entry_id"])] = {"cleared_by": r["reviewer"], "reason": r["reason_text"], "at": r["ts"]}
    return out


async def run(ctx: RunContext) -> None:
    c = _collect(ctx)
    cleared_sanctions = cleared_entries(ctx.submission_id, "sanctions")
    cleared_watch = cleared_entries(ctx.submission_id, "watchlist")
    vendor_id = db.query_one("SELECT vendor_id FROM submissions WHERE id = ?", (ctx.submission_id,))["vendor_id"]

    def sanctions_check():
        screened = [("legal or trade name", n) for n in c["name"]] + [("director", d) for d in c["director"]] + \
                   [("account holder", h) for h in c["holder"]]
        hits = sanctions.screen(screened, sanctions.load_entries(), config.sanctions_fuzzy_threshold)
        # Only near matches can be cleared; an exact match is never suppressed.
        kept = [h for h in hits if not (h.match_type == "fuzzy" and h.entry.id in cleared_sanctions)]
        out = sanctions.sanctions_outcome(kept)
        out.evidence["names_screened"] = len(screened)
        if len(kept) != len(hits):
            out.evidence["previously_cleared"] = [cleared_sanctions[h.entry.id] | {"entry_id": h.entry.id} for h in hits if h not in kept]
        return out

    await ctx.check("sanctions", sanctions_check)

    def watchlist_check():
        candidate = {k: v for k, v in c.items() if k != "holder"}
        all_hits = watchlist.screen(candidate, watchlist.load_entries(clock.today()), config.sanctions_fuzzy_threshold)
        hits = [h for h in all_hits if h.entry.id not in cleared_watch]
        for h in all_hits:
            db.execute("INSERT INTO watchlist_matches(run_id, watchlist_id, matched_kind, match_type, score) VALUES (?,?,?,?,?)",
                       (ctx.run_id, h.entry.id, h.kind, h.match_type, h.score))
        out = watchlist.watchlist_outcome(hits, ctx.form.get("legal_name", ""))
        if len(hits) != len(all_hits):
            out.evidence["previously_cleared"] = [cleared_watch[e] | {"entry_id": e} for e in {h.entry.id for h in all_hits} - {h.entry.id for h in hits}]
        return out

    await ctx.check("watchlist", watchlist_check)
    vendors = vendor_master.load_vendors()
    await ctx.check("duplicate_vendor", lambda: vendor_master.duplicate_outcome(ctx.form.get("tax_id", ""), vendors, vendor_id))
    await ctx.check("bank_collision", lambda: vendor_master.collision_outcome(forms.account(ctx.form), ctx.form.get("legal_name", ""), vendors, vendor_id))
