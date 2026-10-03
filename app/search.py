"""Pluggable web search for tax-rule learning, plus a safe page fetcher.

Providers return candidate sources. The allowlist is enforced here in code (Gemini's search
grounding cannot be restricted to domains): redirects are resolved and only the *final*
hostname counts. Fetched pages are recorded/replayed with the LLM mode so tests and the
offline demo never touch the network.
"""
from __future__ import annotations

import hashlib
import html
import json
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx
import regex

from app import settings_store
from app.config import ROOT, config
from app.llm import tasks
from app.llm.client import RECORDINGS_DIR, CallInfo

FIXTURES = ROOT / "fixtures" / "search"
MAX_PAGE_BYTES = 2_000_000
MAX_PAGE_CHARS = 20_000
CONTEXT_WINDOW = 1_500

# Curated official sources, used when live search is unavailable (e.g. no grounding quota).
DIRECTORY: dict[tuple[str, str], list[tuple[str, str]]] = {
    ("US", "EIN"): [("https://www.irs.gov/pub/irs-pdf/p1635.pdf", "IRS Publication 1635: Understanding your EIN"),
                    ("https://taxmap.irs.gov/taxmap/pubs/p80-001.htm", "IRS Publication 80: Employer Identification Number")],
}

DEFAULT_ALLOWLIST = [
    "irs.gov", "sba.gov", "ssa.gov", "usa.gov",                     # United States
    "gov.uk", "hmrc.gov.uk",                                         # United Kingdom
    "gst.gov.in", "incometax.gov.in", "cbic.gov.in", "gov.in",       # India
    "uen.gov.sg", "iras.gov.sg", "acra.gov.sg", "gov.sg",            # Singapore
    "sii.cl", "gob.cl",                                              # Chile
    "gov.br", "receita.fazenda.gov.br",                             # Brazil
    "bzst.de", "bundesfinanzministerium.de",                         # Germany
    "europa.eu", "oecd.org", "canada.ca", "ato.gov.au", "abr.gov.au",
]


def allowlist() -> list[str]:
    extra = [d.strip().lower() for d in (config.tax_source_allowlist or "").split(",") if d.strip()]
    return DEFAULT_ALLOWLIST + extra


def host_allowed(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    return any(host == d or host.endswith("." + d) for d in allowlist())


@dataclass
class Candidate:
    url: str
    title: str = ""
    text: str | None = None  # fixture providers supply text directly


@dataclass
class SearchResult:
    provider: str
    query: str
    candidates: list[Candidate] = field(default_factory=list)
    info: CallInfo | None = None


_URL_RE = regex.compile(r"https?://[^\s\"'<>)\]]+")


def _collect_urls(obj, out: list[tuple[str, str]]) -> None:
    """Walk the interaction steps and pick up every cited URL (format-tolerant)."""
    if isinstance(obj, dict):
        url = obj.get("url") or obj.get("uri")
        if isinstance(url, str) and url.startswith("http"):
            out.append((url, str(obj.get("title") or "")))
        for v in obj.values():
            _collect_urls(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _collect_urls(v, out)


async def gemini_search(country_name: str, tax_type: str) -> SearchResult:
    raw, info = await tasks.tax_rule_search(country_name, tax_type)
    found: list[tuple[str, str]] = []
    _collect_urls(raw.get("steps", []), found)
    for u in _URL_RE.findall(raw.get("output_text") or ""):
        found.append((u.rstrip(".,;"), ""))
    seen, candidates = set(), []
    for url, title in found:
        if url not in seen:
            seen.add(url)
            candidates.append(Candidate(url, title))
    from app.llm.prompts import tax_rule_search_user
    return SearchResult("gemini", tax_rule_search_user(country_name, tax_type), candidates, info)


def fixture_search(country: str, tax_type: str) -> SearchResult:
    path = FIXTURES / f"{country}_{tax_type}.json"
    if not path.exists():
        return SearchResult("fixture", f"{country} {tax_type} (fixture)")
    data = json.loads(path.read_text(encoding="utf-8"))
    return SearchResult("fixture", data.get("query", f"{country} {tax_type}"),
                        [Candidate(p["url"], p.get("title", ""), p["text"]) for p in data.get("pages", [])])


def directory_search(country: str, tax_type: str) -> SearchResult:
    return SearchResult("directory", f"curated official sources for {country} {tax_type}",
                        [Candidate(url, title) for url, title in DIRECTORY.get((country, tax_type), [])])


async def search(country: str, country_name: str, tax_type: str) -> tuple[SearchResult, str | None]:
    """Returns (result, note). Falls back from live search to the curated directory if search is unavailable."""
    from app.llm.client import LLMUnavailable
    provider = settings_store.get("search_provider")
    if provider == "fixture":
        return fixture_search(country, tax_type), None
    if provider == "directory":
        return directory_search(country, tax_type), None
    try:
        result = await gemini_search(country_name, tax_type)
    except LLMUnavailable as exc:
        return directory_search(country, tax_type), f"Live web search unavailable ({exc.reason}); used curated official sources instead."
    if not any(host_allowed(c.url) for c in result.candidates) and DIRECTORY.get((country, tax_type)):
        fallback = directory_search(country, tax_type)
        fallback.info = result.info
        return fallback, "Live search returned no sources on the allowlist; used curated official sources instead."
    return result, None


def focus(text: str, keywords: list[str]) -> str:
    """Keep the parts of a long page around the identifier's name, so the format statement isn't cut off."""
    if len(text) <= MAX_PAGE_CHARS:
        return text
    spans: list[tuple[int, int]] = []
    for kw in keywords:
        for m in regex.finditer(regex.escape(kw), text, flags=regex.IGNORECASE):
            spans.append((max(0, m.start() - CONTEXT_WINDOW), min(len(text), m.end() + CONTEXT_WINDOW)))
    if not spans:
        return text[:MAX_PAGE_CHARS]
    spans.sort()
    merged = [list(spans[0])]
    for a, b in spans[1:]:
        if a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    out = ""
    for a, b in merged:
        if len(out) >= MAX_PAGE_CHARS:
            break
        out += text[a:b] + "\n…\n"
    return out[:MAX_PAGE_CHARS]


# --- fetching -------------------------------------------------------------------------------------

def html_to_text(raw: str) -> str:
    raw = regex.sub(r"(?is)<(script|style|noscript|svg|head)\b.*?</\1>", " ", raw)
    raw = regex.sub(r"(?s)<!--.*?-->", " ", raw)
    raw = regex.sub(r"(?i)<br\s*/?>|</(p|div|li|tr|h[1-6])>", "\n", raw)
    text = html.unescape(regex.sub(r"<[^>]+>", " ", raw))
    text = regex.sub(r"[ \t\r\f\v]+", " ", text)
    return regex.sub(r"\n\s*\n+", "\n", text).strip()


def pdf_text(data: bytes) -> str:
    import pymupdf
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
        if doc.needs_pass:
            return ""
        text = "\n".join(page.get_text() for page in doc)
        doc.close()
        return regex.sub(r"[ \t]+", " ", text)
    except Exception:
        return ""


def _page_path(url: str):
    return RECORDINGS_DIR / "pages" / f"{hashlib.sha256(url.encode()).hexdigest()[:32]}.json"


@dataclass
class FetchedPage:
    requested_url: str
    final_url: str
    text: str
    allowed: bool
    error: str | None = None


async def fetch(url: str) -> FetchedPage:
    """Resolve redirects, enforce the allowlist on the final host, and return plain text."""
    mode = settings_store.get("llm_mode")
    path = _page_path(url)
    if mode == "replay":
        if not path.exists():
            return FetchedPage(url, url, "", False, "no recorded page (replay mode)")
        d = json.loads(path.read_text(encoding="utf-8"))
        return FetchedPage(url, d["final_url"], d["text"], host_allowed(d["final_url"]), d.get("error"))
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=12, max_redirects=5,
                                     headers={"User-Agent": "VendorOnboardingDemo/1.0 (tax format lookup)"}) as client:
            async with client.stream("GET", url) as resp:
                final = str(resp.url)
                if not host_allowed(final):
                    page = FetchedPage(url, final, "", False, "final host is not on the allowlist")
                else:
                    body = b""
                    async for chunk in resp.aiter_bytes():
                        body += chunk
                        if len(body) > MAX_PAGE_BYTES:
                            break
                    ctype = resp.headers.get("content-type", "")
                    if body.startswith(b"%PDF-"):
                        text = pdf_text(body)
                    else:
                        decoded = body.decode(resp.encoding or "utf-8", errors="replace")
                        text = html_to_text(decoded) if "html" in ctype or "<html" in decoded[:500].lower() else decoded
                    page = FetchedPage(url, final, text[:200_000], True, None if resp.status_code < 400 else f"HTTP {resp.status_code}")
    except httpx.HTTPError as exc:
        page = FetchedPage(url, url, "", False, type(exc).__name__)
    if mode == "record":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"url": url, "final_url": page.final_url, "text": page.text, "error": page.error}, ensure_ascii=False),
                        encoding="utf-8")
    return page
