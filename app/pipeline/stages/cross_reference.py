"""Stage 7: Cross-reference names, account holder, account number and country across form and documents."""
from __future__ import annotations

from app.config import config
from app.domain import account_holder, banking
from app.domain.names import band, match_names, name_outcome
from app.llm import tasks
from app.llm.client import LLMUnavailable
from app.models import failed, passed, skipped
from app.pipeline import forms
from app.pipeline.checks import SLOT_LABEL
from app.pipeline.context import RunContext


async def name_check(ctx: RunContext, a: str, b: str, label: str, page: int | None = None):
    m = match_names(a, b)
    verdict, available, llm_ev = None, True, {}
    if band(m, config.name_fuzzy_ambiguous_low) == "ambiguous" and not m.mixed_script:
        try:
            key = ("names", a, b)
            if key not in ctx.memo:
                ctx.memo[key] = await tasks.adjudicate_names(a, b, ctx.form.get("country", ""), ctx.form.get("entity_type", ""))
            res, info = ctx.memo[key]
            verdict, llm_ev = res.same_entity, {**info.evidence(), "llm_reasons": res.reasons}
        except LLMUnavailable as exc:
            available = False
            ctx.llm_failures.append(f"name adjudication: {exc.reason}")
            llm_ev = {"llm_error": exc.reason}
    out = name_outcome(m, config.name_fuzzy_ambiguous_low, verdict, available, label)
    out.evidence.update(llm_ev)
    if page:
        out.evidence["page"] = page
    return out


async def run(ctx: RunContext) -> None:
    f = ctx.form
    legal = f.get("legal_name", "")

    for slot in ("tax_cert", "incorporation_cert"):
        async def doc_name(s=slot):
            ex, blocker = ctx.usable(s)
            if ex is None:
                if s == "incorporation_cert" and f.get("entity_type") == "sole_proprietor":
                    return skipped("Not applicable to sole proprietors.")
                return skipped(f"The {SLOT_LABEL[s]} could not be used.", blocked_by=blocker)
            if not ex.legal_name:
                return skipped(f"No legal name could be read from the {SLOT_LABEL[s]}.")
            return await name_check(ctx, legal, ex.legal_name, f"Form legal name vs {SLOT_LABEL[s]}", ctx.docs[s].page_of(ex.legal_name))
        await ctx.check(f"name.{slot}", doc_name)

    letter, letter_blocker = ctx.usable("bank_letter")

    async def holder_name():
        if letter is None:
            return skipped("The bank letter could not be used.", blocked_by=letter_blocker)
        if not letter.account_holder:
            return skipped("No account holder could be read from the bank document.")
        return await name_check(ctx, f.get("bank_account_holder", ""), letter.account_holder,
                                "Account holder on the form vs bank document", ctx.docs["bank_letter"].page_of(letter.account_holder))

    await ctx.check("name.bank_holder", holder_name)

    def account_match():
        if letter is None:
            return skipped("The bank letter could not be used.", blocked_by=letter_blocker)
        on_letter = banking.normalize_account(letter.iban or letter.account_number or "")
        on_form = banking.normalize_account(forms.account(f))
        ev = {"form_account": on_form, "letter_account": on_letter, "page": ctx.docs["bank_letter"].page_of(letter.iban or letter.account_number)}
        if not on_letter:
            return skipped("No account number could be read from the bank document.", **ev)
        if on_letter == on_form:
            return passed("The account number on the form matches the bank document.", **ev)
        return failed("BANK_DETAILS_MISMATCH", f"The form gives account {on_form}, but the bank document shows {on_letter}. "
                      "Payments must only go to a verified account.", **ev)

    await ctx.check("bank_matches_letter", account_match)

    async def holder_type():
        holder = (letter.account_holder if letter else None) or f.get("bank_account_holder", "")
        if letter is None and not holder:
            return skipped("No account holder available.", blocked_by=letter_blocker)
        directors: list[str] = []
        for slot in ("incorporation_cert", "tax_cert"):
            ex, _ = ctx.usable(slot)
            if ex:
                directors += [d for d in ex.directors_or_owners if d not in directors]
        a = account_holder.assess_holder(holder, legal, directors, letter.account_type if letter else "unknown")
        if a.label == "unclear":
            try:
                res, info = await tasks.adjudicate_holder(holder, f.get("entity_type", ""), letter.account_type if letter else "unknown")
                account_holder.apply_llm(a, res.label)
            except LLMUnavailable as exc:
                ctx.llm_failures.append(f"holder adjudication: {exc.reason}")
                a.llm_consulted = False
        anomalies = sorted({r.reason_code for r in ctx.results.values()
                            if r.result == "fail" and r.severity in ("escalate", "reject") and r.reason_code != "LLM_UNAVAILABLE"})
        out = account_holder.holder_outcome(a, f.get("entity_type", ""), directors, anomalies)
        out.evidence["directors_on_documents"] = directors
        return out

    await ctx.check("holder_type", holder_type)

    def country_consistency():
        declared = f.get("country", "")
        seen: dict[str, str] = {}
        for slot in ("tax_cert", "incorporation_cert", "bank_letter"):
            ex, _ = ctx.usable(slot)
            if ex and ex.country and len(ex.country) == 2:
                seen[SLOT_LABEL[slot]] = ex.country.upper()
        address = f.get("registered_address", "").lower()
        for code, name in forms.COUNTRIES.items():
            if code != declared and name.lower() in address:
                seen["registered address"] = code
        mismatched = {k: v for k, v in seen.items() if v != declared}
        if mismatched:
            listed = ", ".join(f"{k}: {v}" for k, v in mismatched.items())
            return failed("COUNTRY_MISMATCH", f"Declared country is {declared}, but {listed}.", declared=declared, seen=seen)
        return passed(f"Country {declared} is consistent across {', '.join(seen) or 'the form'}.", declared=declared, seen=seen)

    await ctx.check("country_consistency", country_consistency)
