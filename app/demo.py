"""Demo lifecycle: boot-time setup and "Reset demo data"."""
from __future__ import annotations

import shutil

from app import audit, db
from app.config import config
from app.seed import seed


def boot() -> None:
    db.init_db()
    config.uploads_dir.mkdir(parents=True, exist_ok=True)
    seed()
    from app.pipeline.runner import recover_interrupted_runs
    recover_interrupted_runs()


def reset(actor: str) -> None:
    """Drop the database and uploads, then re-seed. The audit log restarts with this event."""
    db.drop_db()
    shutil.rmtree(config.uploads_dir, ignore_errors=True)
    boot()
    audit.log(actor, "reset_demo_data", "database", None, {})
