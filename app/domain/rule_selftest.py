"""Self-test a tax-ID regex before it is trusted (brief §10, step 5).

The rule must accept every example found on the source page, reject almost all
random strings and a junk battery, be short, avoid catastrophic backtracking
shapes, and never take more than the regex timeout on any probe.
"""
from __future__ import annotations

import random
import string
import time
from dataclasses import dataclass, field

import regex

from app.domain.taxids import REGEX_TIMEOUT_SECONDS

MAX_REGEX_LEN = 200
RANDOM_PROBES = 200
MIN_REJECT_RATE = 0.95

# Nested quantifiers such as (a+)+, (\d*)*, (.*)+ are the classic catastrophic-backtracking shapes.
_NESTED_QUANTIFIER = regex.compile(r"\((?:[^()\\]|\\.)*[+*}](?:[^()\\]|\\.)*\)\s*[+*{]")
_TOO_PERMISSIVE = regex.compile(r"^\^?\(?\.[*+]\)?\$?$")


@dataclass
class SelfTestReport:
    passed: bool
    reasons: list[str] = field(default_factory=list)
    examples_matched: int = 0
    examples_total: int = 0
    random_reject_rate: float = 0.0
    junk_rejected: bool = False
    max_probe_ms: float = 0.0

    def as_dict(self) -> dict:
        return {
            "passed": self.passed, "reasons": self.reasons,
            "examples_matched": f"{self.examples_matched}/{self.examples_total}",
            "random_reject_rate": round(self.random_reject_rate, 3), "junk_rejected": self.junk_rejected,
            "max_probe_ms": round(self.max_probe_ms, 2),
        }


def _probe(compiled: regex.Pattern, value: str) -> tuple[bool, float, bool]:
    """(matched, elapsed_ms, timed_out)"""
    start = time.perf_counter()
    try:
        matched = compiled.fullmatch(value, timeout=REGEX_TIMEOUT_SECONDS) is not None
        timed_out = False
    except TimeoutError:
        matched, timed_out = False, True
    return matched, (time.perf_counter() - start) * 1000, timed_out


def self_test(pattern: str, examples: list[str], seed: int = 1234) -> SelfTestReport:
    report = SelfTestReport(passed=False, examples_total=len(examples))
    if not pattern or len(pattern) > MAX_REGEX_LEN:
        report.reasons.append(f"regex is empty or longer than {MAX_REGEX_LEN} characters")
        return report
    if _TOO_PERMISSIVE.match(pattern.strip()):
        report.reasons.append("regex accepts anything")
        return report
    if _NESTED_QUANTIFIER.search(pattern):
        report.reasons.append("regex has nested quantifiers (catastrophic backtracking risk)")
        return report
    try:
        compiled = regex.compile(pattern)
    except regex.error as exc:
        report.reasons.append(f"regex does not compile: {exc}")
        return report
    if not examples:
        report.reasons.append("no example IDs were found on the source page to test against")
        return report

    timed_out_any = False
    for ex in examples:
        ok, ms, to = _probe(compiled, ex)
        report.max_probe_ms = max(report.max_probe_ms, ms)
        timed_out_any |= to
        report.examples_matched += ok
    if report.examples_matched != len(examples):
        report.reasons.append(f"regex matched only {report.examples_matched} of {len(examples)} example IDs")

    rng = random.Random(seed)
    lengths = sorted({len(e) for e in examples})
    alphabet = string.ascii_uppercase + string.digits + "-./ "
    rejected = 0
    for i in range(RANDOM_PROBES):
        n = lengths[i % len(lengths)]
        candidate = "".join(rng.choice(alphabet) for _ in range(n))
        ok, ms, to = _probe(compiled, candidate)
        report.max_probe_ms = max(report.max_probe_ms, ms)
        timed_out_any |= to
        rejected += not ok
    report.random_reject_rate = rejected / RANDOM_PROBES
    if report.random_reject_rate < MIN_REJECT_RATE:
        report.reasons.append(f"regex accepted {100 - report.random_reject_rate * 100:.1f}% of random strings (max allowed 5%)")

    longest = max(lengths)
    junk = ["", "A" * longest, "0" * longest, "9" * (longest * 4), "A" * 5000, "a1" * 2000, "-" * longest]
    junk_hits = 0
    for j in junk:
        ok, ms, to = _probe(compiled, j)
        report.max_probe_ms = max(report.max_probe_ms, ms)
        timed_out_any |= to
        junk_hits += ok
    # A short all-zero / all-letter string could be structurally valid for some formats; allow at most one junk hit.
    report.junk_rejected = junk_hits <= 1
    if not report.junk_rejected:
        report.reasons.append(f"regex accepted {junk_hits} junk inputs")
    if timed_out_any:
        report.reasons.append(f"a probe exceeded the {REGEX_TIMEOUT_SECONDS}s regex timeout")

    report.passed = not report.reasons
    return report
