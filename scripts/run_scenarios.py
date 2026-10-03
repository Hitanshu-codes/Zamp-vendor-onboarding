"""Run sample scenarios through the real pipeline on a fresh scratch database.

    python scripts/run_scenarios.py record S0 S1 S3     # live Gemini, save recordings
    python scripts/run_scenarios.py replay all          # offline, from recordings
    python scripts/run_scenarios.py replay all -v       # also print failing/warning checks
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="vo-scen-"))
os.environ.setdefault("EMAIL_PROVIDER", "outbox")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db, demo, scenarios, settings_store  # noqa: E402


async def main(mode: str, ids: list[str], verbose: bool) -> int:
    demo.boot()
    settings_store.set_("llm_mode", mode)
    failures = 0
    for sid in ids:
        r = await scenarios.run(sid)
        failures += not r.ok
        print(r.line(), flush=True)
        if verbose or not r.ok:
            for row in db.query("SELECT * FROM check_results WHERE run_id = ? AND result IN ('fail','warn')", (r.run_id,)):
                print(f"    {row['check_name']}: {row['result']} {row['reason_code'] or ''} - {row['explanation'][:160]}")
    print(f"\n{len(ids) - failures}/{len(ids)} scenarios as expected")
    return failures


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    ids = scenarios.available() if args[1:] == ["all"] else args[1:]
    sys.exit(1 if asyncio.run(main(args[0], ids, "-v" in sys.argv)) else 0)
