"""Prompt-injection defence: deterministic pattern scan, hidden-text detection, sanitisers.

The main defence is architectural (the LLM never decides anything). This module
only *detects* attempts so they can be escalated and shown to a reviewer.
"""
from __future__ import annotations

import unicodedata
from dataclasses import asdict, dataclass

import pymupdf
import regex

# --- normalisation ------------------------------------------------------------

ZERO_WIDTH = regex.compile(r"[​-‏‪-‮⁠-⁤﻿­]")
# Common Cyrillic/Greek look-alikes mapped to Latin so "іgnore" (Cyrillic і) still matches.
HOMOGLYPHS = str.maketrans({
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "у": "y", "х": "x", "і": "i", "ј": "j", "ѕ": "s", "ԁ": "d",
    "ɡ": "g", "ո": "n", "ν": "v", "ο": "o", "α": "a", "ε": "e", "ι": "i", "κ": "k", "ρ": "p", "τ": "t", "υ": "u",
    "А": "A", "В": "B", "Е": "E", "К": "K", "М": "M", "Н": "H", "О": "O", "Р": "P", "С": "C", "Т": "T", "Х": "X",
    "0": "0", "@": "a",
})


def normalize_for_scan(text: str) -> str:
    t = unicodedata.normalize("NFKC", text or "")
    t = ZERO_WIDTH.sub("", t)
    t = t.translate(HOMOGLYPHS).lower()
    return regex.sub(r"\s+", " ", t)


def squash(text: str) -> str:
    """Letters only, to catch spaced-out tricks like 'i g n o r e  p r e v i o u s'."""
    return regex.sub(r"[^a-z]", "", normalize_for_scan(text))


# --- deterministic patterns ------------------------------------------------------

PATTERNS: list[tuple[str, str]] = [
    ("ignore_instructions", r"\bignore\s+(?:all\s+|any\s+|the\s+)?(?:previous\s+|prior\s+|above\s+|earlier\s+|other\s+)?(?:instructions|rules|checks|guidelines|prompts?)\b"),
    ("disregard", r"\bdisregard\s+(?:all\s+|any\s+|the\s+)?(?:previous\s+|prior\s+|above\s+|earlier\s+)?(?:instructions|rules|checks|guidelines|prompts?)\b"),
    ("role_override", r"\byou\s+are\s+now\s+(?:a|an|the|in|acting)\b"),
    ("system_prompt", r"\bsystem\s+prompt\b"),
    ("approve_vendor", r"\bapprove\s+(?:this|the)\s+(?:vendor|supplier|submission|application|company|onboarding)\b"),
    ("mark_approved", r"\bmark\s+(?:\w+\s+){0,3}?(?:as\s+)?approved\b"),
    ("set_status", r"\bset\s+(?:the\s+)?(?:status|decision)\s+to\s+\w+"),
    ("skip_checks", r"\bskip\s+(?:the\s+|all\s+|any\s+)?(?:verification|checks?|validation|review|screening)\b"),
    ("do_not_flag", r"\bdo\s+not\s+(?:flag|escalate|review|reject|report)\b"),
    ("as_an_ai", r"\bas\s+an\s+(?:ai|llm|language\s+model)\b"),
    ("addressed_imperative", r"\b(?:assistant|reviewer|model|ai|llm|chatbot|system|gemini|claude)\s*[,:]\s*(?:please\s+)?(?:approve|ignore|skip|mark|set|override|disregard|accept)\b"),
    ("override", r"\boverride\s+(?:the\s+|all\s+|any\s+)?(?:decision|status|rules|checks|instructions)\b"),
    ("new_instructions", r"\bnew\s+instructions?\s*:"),
]
_COMPILED = [(name, regex.compile(p)) for name, p in PATTERNS]
SQUASHED = ["ignorepreviousinstructions", "ignoreallinstructions", "ignoreallrules", "ignoreallpreviousinstructions",
            "approvethisvendor", "markasapproved", "skipverification", "skipallchecks", "donotflag", "systemprompt",
            "disregardallinstructions", "disregardpreviousinstructions"]


@dataclass
class Finding:
    source: str          # e.g. "tax_cert: visible text, page 1", "form field: legal_name", "tax_cert: metadata title"
    method: str          # pattern | squashed | hidden_text | llm
    rule: str
    excerpt: str         # raw text around the hit; always rendered escaped
    detail: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def scan_text(text: str, source: str) -> list[Finding]:
    if not text or not text.strip():
        return []
    norm = normalize_for_scan(text)
    findings: list[Finding] = []
    for name, rx in _COMPILED:
        m = rx.search(norm)
        if m:
            s, e = max(0, m.start() - 60), min(len(norm), m.end() + 60)
            findings.append(Finding(source, "pattern", name, norm[s:e].strip()))
    if not findings:
        sq = squash(text)
        for word in SQUASHED:
            if word in sq:
                findings.append(Finding(source, "squashed", word, text.strip()[:160], "letters spaced out or obfuscated"))
                break
    return findings


# --- PDF text, hidden text and metadata -------------------------------------------

@dataclass
class HiddenSpan:
    page: int
    text: str
    reasons: list[str]
    size: float
    color: tuple
    bbox: tuple

    def describe(self) -> str:
        r, g, b = (round(c, 2) for c in self.color[:3]) if self.color else (0, 0, 0)
        colour = "white" if min(r, g, b) > 0.9 else f"rgb({r},{g},{b})"
        return f"hidden text, page {self.page}, {self.size:.0f}pt {colour} ({', '.join(self.reasons)})"


@dataclass
class PdfTextReport:
    page_count: int
    visible_text: list[str]        # per page
    hidden: list[HiddenSpan]
    ocr_layer_text: list[str]      # render-mode-3 text (normal for scanned PDFs, scanned but not flagged by itself)
    metadata: dict[str, str]


def _bg_colour_at(page: pymupdf.Page, rect: pymupdf.Rect, fills: list) -> tuple:
    centre = pymupdf.Point((rect.x0 + rect.x1) / 2, (rect.y0 + rect.y1) / 2)
    colour = (1.0, 1.0, 1.0)
    for fill, frect in fills:  # later drawings paint over earlier ones
        if frect.contains(centre):
            colour = fill
    return colour


def _close(a: tuple, b: tuple, tol: float = 0.12) -> bool:
    return all(abs(x - y) <= tol for x, y in zip(a[:3], b[:3]))


def analyse_pdf(data: bytes) -> PdfTextReport:
    doc = pymupdf.open(stream=data, filetype="pdf")
    try:
        visible: list[str] = []
        hidden: list[HiddenSpan] = []
        ocr: list[str] = []
        for pno, page in enumerate(doc, start=1):
            fills = [(d["fill"], d["rect"]) for d in page.get_drawings() if d.get("fill") is not None]
            page_rect = page.rect
            page_visible: list[str] = []
            for span in page.get_texttrace():
                text = "".join(chr(c[0]) for c in span["chars"]).strip()
                if not text:
                    continue
                rect = pymupdf.Rect(span["bbox"])
                color = tuple(span.get("color") or (0, 0, 0))
                if len(color) == 1:
                    color = (color[0],) * 3
                reasons: list[str] = []
                if span.get("type") == 3:
                    ocr.append(text)
                    continue
                if span["size"] < 3:
                    reasons.append("font under 3pt")
                if (span.get("opacity") if span.get("opacity") is not None else 1.0) < 0.05:
                    reasons.append("fully transparent")
                if not rect.intersects(page_rect) or (rect & page_rect).get_area() < 0.5 * max(rect.get_area(), 1e-6):
                    reasons.append("outside the page")
                if len(color) >= 3 and _close(color, _bg_colour_at(page, rect, fills)):
                    reasons.append("same colour as background")
                if reasons:
                    hidden.append(HiddenSpan(pno, text, reasons, float(span["size"]), color, tuple(round(x, 1) for x in span["bbox"])))
                else:
                    page_visible.append(text)
            visible.append(" ".join(page_visible))
        meta = {k: v for k, v in (doc.metadata or {}).items() if k in ("title", "subject", "keywords", "author", "creator", "producer") and v}
        xmp = doc.get_xml_metadata() or ""
        if xmp.strip():
            meta["xmp"] = regex.sub(r"<[^>]+>", " ", xmp)
        return PdfTextReport(doc.page_count, visible, hidden, ocr, meta)
    finally:
        doc.close()


CONCEALED_MIN_WORDS = 3


def scan_pdf_report(report: PdfTextReport, slot: str) -> list[Finding]:
    findings: list[Finding] = []
    for i, text in enumerate(report.visible_text, start=1):
        findings += scan_text(text, f"{slot}: visible text, page {i}")
    for span in report.hidden:
        hits = scan_text(span.text, f"{slot}: {span.describe()}")
        if hits:
            findings += hits
        elif len(span.text.split()) >= CONCEALED_MIN_WORDS:
            # Deliberately concealed wording is itself an attempt to influence automated readers.
            findings.append(Finding(f"{slot}: {span.describe()}", "hidden_text", "concealed_text", span.text[:200],
                                    "text is present in the file but not visible to a human reader"))
    ocr_text = " ".join(report.ocr_layer_text)
    findings += scan_text(ocr_text, f"{slot}: invisible text layer (render mode 3)")
    for key, value in report.metadata.items():
        findings += scan_text(value, f"{slot}: metadata {key}")
    return findings


def all_text_for_llm_scan(report: PdfTextReport) -> str:
    parts = [*report.visible_text, *(s.text for s in report.hidden), " ".join(report.ocr_layer_text), *report.metadata.values()]
    return "\n".join(p for p in parts if p.strip())


# --- sanitisers ----------------------------------------------------------------

SAFE_NAME = regex.compile(r"[^A-Za-z0-9 .,&'\-]")


def sanitize_name(value: str | None, limit: int = 80) -> str:
    """The only vendor-supplied text allowed into vendor emails (INV-3)."""
    t = unicodedata.normalize("NFKD", value or "")
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = SAFE_NAME.sub("", t)
    return " ".join(t.split())[:limit].strip()


def sanitize_filename(name: str | None) -> str:
    base = (name or "upload").replace("\\", "/").rsplit("/", 1)[-1]
    base = regex.sub(r"[^A-Za-z0-9._-]", "_", base).strip("._") or "upload"
    return base[:100]
