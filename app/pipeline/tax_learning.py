"""Learn a tax-ID format rule on a registry miss (brief §10).

search → keep only allow-listed sources (checked on the final host after redirects) →
injection-scan each page → LLM extracts {regex, examples, templates} from the page as untrusted
data (no tools) → keep only examples/templates printed verbatim on the page → self-test →
store as *provisional*. Any failure leaves no rule, so the tax ID goes to a person.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from urllib.parse import urlparse

import regex

from app import clock, db, search, tax_registry
from app.domain.rule_selftest import self_test
from app.domain.taxids import TaxRule
from app.llm import tasks
from app.llm.client import LLMUnavailable
from app.pipeline import forms
from app.security.injection import scan_text

MAX_SOURCES = 3
REDIRECTORS = ("vertexaisearch.cloud.google.com",)  # Gemini citation links redirect through here
CACHEABLE_MISSES = ("not_found", "failed_selftest", "rejected_injection")


@dataclass
class LearnResult:
    rule: TaxRule | None
    summary: str
    evidence: dict = field(default_factory=dict)


def recent_miss(country: str, tax_type: str) -> bool:
    """Negative cache: a genuine miss within 24h is not searched again (errors are not cached)."""
    since = (clock.now() - timedelta(hours=24)).isoformat(timespec="seconds")
    marks = ",".join("?" * len(CACHEABLE_MISSES))
    row = db.query_one(f"SELECT 1 FROM tax_rule_search_log WHERE country = ? AND tax_type = ? AND outcome IN ({marks}) AND ts >= ?",
                       (country, tax_type, *CACHEABLE_MISSES, since))
    return row is not None


def template_examples(template: str) -> str:
    """Turn a printed template like 'XX-XXXXXXX' into a concrete sample ('47-2918365') for the self-test."""
    digits, letters = "4729183650", "KQRTVW"
    out, di, li = [], 0, 0
    for ch in template:
        if ch in "XN9#0":
            out.append(digits[di % len(digits)])
            di += 1
        elif ch in "AL":
            out.append(letters[li % len(letters)])
            li += 1
        else:
            out.append(ch)
    return "".join(out)


def _squash(s: str) -> str:
    return regex.sub(r"\s+", "", s or "").upper()


def _log(country: str, tax_type: str, query: str, outcome: str, urls: list[str], notes: str) -> None:
    db.execute("INSERT INTO tax_rule_search_log(country, tax_type, query, outcome, ts, source_urls_json, notes) VALUES (?,?,?,?,?,?,?)",
               (country, tax_type, query[:500], outcome, clock.now_iso(), db.dumps(urls), notes[:500]))


async def learn(ctx, country: str, tax_type: str) -> LearnResult:
    if recent_miss(country, tax_type):
        return LearnResult(None, "A search for this rule failed in the last 24 hours, so it was not repeated (negative cache).",
                           {"negative_cache": True})
    country_name = forms.COUNTRIES.get(country, country)
    ctx.info(f"No {country} {tax_type} rule in the registry. Looking for the official format on the web…")
    try:
        result, note = await search.search(country, country_name, tax_type)
    except Exception as exc:  # defensive: search must never crash the run
        _log(country, tax_type, f"{country} {tax_type}", "error", [], type(exc).__name__)
        return LearnResult(None, "The web search failed.", {"search_error": type(exc).__name__})
    if note:
        ctx.info(note)

    sources: list[dict] = []
    evidence: dict = {"provider": result.provider, "query": result.query, "sources": sources, "note": note}
    if result.info:
        evidence.update({k: v for k, v in result.info.evidence().items() if k in ("llm_model", "llm_latency_ms", "llm_source")})
    usable = []
    for c in result.candidates:
        host = (urlparse(c.url).hostname or "").lower()
        if search.host_allowed(c.url) or host.endswith(REDIRECTORS):
            usable.append(c)
        else:
            sources.append({"url": c.url, "host": host, "outcome": "skipped: not on the allowlist"})
    ctx.info(f"{len(result.candidates)} source(s) found via {result.provider}; {len(usable)} eligible "
             f"(official domains only).", sources=[c.url for c in usable[:MAX_SOURCES]])

    outcomes: set[str] = set()
    for c in usable[:MAX_SOURCES]:
        rec = {"url": c.url}
        sources.append(rec)
        if c.text is not None:
            final_url, text, allowed, err = c.url, c.text, search.host_allowed(c.url), None
        else:
            page = await search.fetch(c.url)
            final_url, text, allowed, err = page.final_url, page.text, page.allowed, page.error
        rec.update({"final_url": final_url, "host": urlparse(final_url).hostname})
        if not allowed:
            rec["outcome"] = "skipped: final host not on the allowlist"
            continue
        if err or not text.strip():
            rec["outcome"] = f"unreachable ({err or 'empty page'})"
            continue
        hits = scan_text(text, f"web page {urlparse(final_url).hostname}")
        if hits:
            rec["outcome"] = f"rejected: page contains instruction-like text ({hits[0].rule})"
            rec["excerpt"] = hits[0].excerpt[:200]
            outcomes.add("rejected_injection")
            ctx.info(f"Rejected {rec['host']}: the page contains instruction-like text, so nothing from it is used.")
            continue
        page_text = search.focus(text, [tax_type, country_name, "format", "digit"])
        try:
            ex, info = await tasks.tax_rule_extract(country, tax_type, final_url, page_text)
        except LLMUnavailable as exc:
            ctx.llm_failures.append(f"tax rule extraction: {exc.reason}")
            rec["outcome"] = f"LLM unavailable ({exc.reason})"
            outcomes.add("error")
            break
        rec["extracted"] = {"found": ex.found, "regex": ex.regex, "examples": ex.example_valid_ids, "templates": ex.format_templates}
        if not ex.found or not ex.regex:
            rec["outcome"] = "no format stated on this page"
            outcomes.add("not_found")
            continue
        squashed_page = _squash(page_text)
        examples = [e for e in ex.example_valid_ids if e and _squash(e) in squashed_page]
        templates = [t for t in ex.format_templates if t and _squash(t) in squashed_page]
        synthetic = [template_examples(t) for t in templates]
        rec["verified_on_page"] = {"examples": examples, "templates": templates, "synthetic_examples": synthetic}
        report = self_test(ex.regex, examples + synthetic)
        rec["selftest"] = report.as_dict()
        if not report.passed:
            rec["outcome"] = "failed self-test: " + "; ".join(report.reasons)
            outcomes.add("failed_selftest")
            ctx.info(f"Rule from {rec['host']} failed the self-test ({'; '.join(report.reasons)}). Not stored.")
            continue
        example = (examples or synthetic)[0]
        rule = tax_registry.store_learned(country, tax_type, ex.description or f"{country_name} {tax_type} (learned)", ex.regex, example,
                                          final_url, ex.checksum_algorithm_name,
                                          f"Learned via {result.provider}; examples on page: {len(examples)}, templates: {len(templates)}.")
        rec["outcome"] = "learned"
        _log(country, tax_type, result.query, "found", [s.get("final_url", s["url"]) for s in sources], f"rule #{rule.id}")
        ctx.info(f"Learned {tax_type} format from {rec['host']}: {ex.regex}. Self-test passed "
                 f"({report.examples_matched}/{report.examples_total} examples, {report.random_reject_rate:.0%} of random strings rejected). "
                 "Stored as a provisional rule.", rule_id=rule.id)
        evidence.update({"selftest": report.as_dict(), "learned_from": final_url})
        return LearnResult(rule, f"Searched {len(usable)} official source(s), learned the format from {rec['host']}, passed the self-test, "
                                 "and stored it as provisional.", evidence)

    if "error" in outcomes:
        outcome = "error"
    elif "failed_selftest" in outcomes:
        outcome = "failed_selftest"
    elif "rejected_injection" in outcomes:
        outcome = "rejected_injection"
    else:
        outcome = "not_found"
    _log(country, tax_type, result.query, outcome, [s.get("final_url", s["url"]) for s in sources],
         "; ".join(s.get("outcome", "") for s in sources)[:500])
    summary = {"error": "The LLM was unavailable while reading the sources.",
               "failed_selftest": "A candidate rule was found but failed the self-test, so it was not trusted.",
               "rejected_injection": "The only candidate sources contained instruction-like text and were rejected.",
               "not_found": "No official source stating the format was found."}[outcome]
    return LearnResult(None, summary, evidence)
