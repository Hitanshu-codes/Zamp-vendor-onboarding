"""Stage 6: Deterministic validation of tax ID, bank details, dates and email."""
from __future__ import annotations

from app import clock
from app.config import config
from app.domain import banking, validators
from app.domain.taxids import gstin_pan, gstin_pan_outcome, interpret, normalize_tax_id, pan_entity_outcome
from app.models import failed, passed, skipped
from app.pipeline import forms
from app.pipeline.checks import SLOT_LABEL
from app.pipeline.context import RunContext
from app.pipeline.stages.tax_rule_lookup import all_active_rules


def _india_gstin(ctx: RunContext) -> bool:
    return ctx.form.get("country") == "IN" and ctx.tax_type == "GSTIN"


async def run(ctx: RunContext) -> None:
    f = ctx.form
    tax_id = normalize_tax_id(f.get("tax_id", ""))

    def tax_format():
        if not tax_id:
            return skipped("No tax ID on the form.", blocked_by="form_complete")
        if ctx.tax_rule is None:
            return skipped("No format rule is available for this country.", blocked_by="tax_rule")
        others = [r for r in all_active_rules() if r.id != ctx.tax_rule.id]
        return interpret(tax_id, ctx.tax_rule, others)

    await ctx.check("tax_id_format", tax_format)

    def matches_cert():
        ex, blocker = ctx.usable("tax_cert")
        if ex is None:
            return skipped("The tax certificate could not be used.", blocked_by=blocker)
        on_cert = [normalize_tax_id(v) for v in (ex.tax_id, ex.secondary_tax_id) if v]
        ev = {"form_tax_id": tax_id, "certificate_tax_ids": on_cert, "page": ctx.docs["tax_cert"].page_of(ex.tax_id)}
        if not on_cert:
            return skipped("The tax certificate shows no tax ID to compare with the form.", **ev)
        key = lambda v: "".join(ch for ch in v if ch.isalnum())  # "47-2918365" and "472918365" are the same ID
        if key(tax_id) in {key(c) for c in on_cert}:
            return passed(f"The tax ID on the form ({tax_id}) matches the certificate.", **ev)
        close = [c for c in on_cert if len(key(c)) == len(key(tax_id)) and sum(a != b for a, b in zip(key(c), key(tax_id))) == 1]
        if close:
            return failed("TAXID_CHECKSUM_FAIL", f"The form says {tax_id} but the certificate says {close[0]}: one character differs, likely a typo.", **ev)
        return failed("TAXID_INCONSISTENT", f"The form's tax ID ({tax_id}) does not match the certificate ({', '.join(on_cert)}).", **ev)

    await ctx.check("tax_id_matches_cert", matches_cert)

    def gstin_pan_check():
        if not _india_gstin(ctx):
            return skipped("Only applies to Indian GSTINs.")
        if len(tax_id) != 15:
            return skipped("GSTIN is not 15 characters, so its PAN cannot be read.", blocked_by="tax_id_format")
        ex, blocker = ctx.usable("tax_cert")
        pan = normalize_tax_id(ex.secondary_tax_id) if ex and ex.secondary_tax_id and len(normalize_tax_id(ex.secondary_tax_id)) == 10 else None
        if pan is None:
            return passed(f"The GSTIN embeds PAN {gstin_pan(tax_id)}; the documents show no separate PAN to compare.", pan_in_gstin=gstin_pan(tax_id))
        return gstin_pan_outcome(tax_id, pan)

    await ctx.check("gstin_pan", gstin_pan_check)

    def pan_entity():
        if not _india_gstin(ctx):
            return skipped("Only applies to Indian GSTINs.")
        if len(tax_id) != 15:
            return skipped("GSTIN is not 15 characters, so its PAN cannot be read.", blocked_by="tax_id_format")
        return pan_entity_outcome(gstin_pan(tax_id), f.get("entity_type", ""))

    await ctx.check("pan_entity", pan_entity)

    acct = forms.account(f)
    await ctx.check("bank_format", lambda: banking.validate_bank(f.get("country", ""), acct, f.get("bank_code", ""))
                    if acct and f.get("bank_code") else skipped("Bank details missing from the form.", blocked_by="form_complete"))
    await ctx.check("bank_country", lambda: banking.bank_country_outcome(
        f.get("country", ""), acct, f.get("bank_code", ""), exempt=f.get("vendor_category") in forms.FOREIGN_ACCOUNT_EXEMPT))

    today = clock.today()
    for slot in ("tax_cert", "incorporation_cert"):
        def expiry(s=slot):
            ex, blocker = ctx.usable(s)
            if ex is None:
                return skipped(f"The {SLOT_LABEL[s]} could not be used.", blocked_by=blocker)
            return validators.expiry_outcome(SLOT_LABEL[s], ex.expiry_date, today)
        await ctx.check(f"doc_expiry.{slot}", expiry)

    def letter_age():
        ex, blocker = ctx.usable("bank_letter")
        if ex is None:
            return skipped("The bank letter could not be used.", blocked_by=blocker)
        return validators.bank_letter_age_outcome(ex.issue_date, today, config.bank_letter_max_age_days)

    await ctx.check("bank_letter_age", letter_age)

    def date_order():
        tax, _ = ctx.usable("tax_cert")
        inc, _ = ctx.usable("incorporation_cert")
        return validators.date_order_outcome(tax.registration_date if tax else None, inc.incorporation_date if inc else None)

    await ctx.check("date_order", date_order)
    await ctx.check("contact_email", lambda: validators.email_outcome(f.get("contact_email", ""), f.get("entity_type", "")))
