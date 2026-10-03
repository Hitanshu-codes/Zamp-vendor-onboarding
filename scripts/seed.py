"""Create the schema and seed reference data. Safe to run repeatedly."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import init_db  # noqa: E402
from app.seed import seed  # noqa: E402

if __name__ == "__main__":
    init_db()
    result = seed()
    print("inserted:", result or "nothing (already seeded)")
