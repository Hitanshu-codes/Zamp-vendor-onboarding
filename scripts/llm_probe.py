"""Exercise the LLM layer on generated samples and print what came back.

    python scripts/llm_probe.py record   # live calls, saved to recordings/
    python scripts/llm_probe.py replay   # saved responses only
    python scripts/llm_probe.py killswitch
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="vo-probe-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import demo, settings_store  # noqa: E402
from app.llm import tasks  # noqa: E402
from app.llm.client import LLMUnavailable  # noqa: E402
from app.security.injection import all_text_for_llm_scan, analyse_pdf  # noqa: E402

SAMPLES = Path(__file__).resolve().parent.parent / "samples"
CASES = [("S0", "tax_cert"), ("S0", "bank_letter"), ("S0", "incorporation_cert"), ("S1", "tax_cert"), ("S2", "bank_letter")]


async def run(mode: str) -> None:
    demo.boot()
    settings_store.set_("llm_mode", "replay" if mode == "killswitch" else mode)
    if mode == "killswitch":
        settings_store.set_("llm_enabled", "false")

    async def one(sid: str, slot: str):
        data = (SAMPLES / sid / f"{slot}.pdf").read_bytes()
        try:
            ex, info = await tasks.classify_extract(data, "application/pdf", slot)
            return f"{sid}/{slot}: {ex.doc_type} ({ex.type_confidence:.2f}) name={ex.legal_name!r} tax={ex.tax_id!r} " \
                   f"holder={ex.account_holder!r} acct={ex.account_number!r} type={ex.account_type} quality={ex.quality} " \
                   f"instr={ex.contains_instructions} [{info.source} {info.latency_ms}ms]"
        except LLMUnavailable as e:
            return f"{sid}/{slot}: LLM_UNAVAILABLE ({e.reason})"

    async def scan(sid: str, slot: str):
        text = all_text_for_llm_scan(analyse_pdf((SAMPLES / sid / f"{slot}.pdf").read_bytes()))
        try:
            res, info = await tasks.injection_scan(f"{slot} text", text)
            return f"scan {sid}/{slot}: contains_instructions={res.contains_instructions} [{info.source} {info.latency_ms}ms]"
        except LLMUnavailable as e:
            return f"scan {sid}/{slot}: LLM_UNAVAILABLE ({e.reason})"

    async def names():
        try:
            r, info = await tasks.adjudicate_names("Acme Industries Private Limited", "Acme Industrial Private Limited", "IN", "company")
            r2, _ = await tasks.adjudicate_names("Acme Pvt. Ltd.", "Acme Private Limited", "IN", "company")
            return f"names: Industries/Industrial={r.same_entity}, Pvt Ltd/Private Limited={r2.same_entity} [{info.source}]"
        except LLMUnavailable as e:
            return f"names: LLM_UNAVAILABLE ({e.reason})"

    results = await asyncio.gather(*(one(s, d) for s, d in CASES), scan("S5b", "bank_letter"), scan("S0", "tax_cert"), names())
    print("\n".join(results))


if __name__ == "__main__":
    asyncio.run(run(sys.argv[1] if len(sys.argv) > 1 else "replay"))
