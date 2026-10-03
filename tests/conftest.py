import os
import tempfile
from pathlib import Path

# Must be set before app.config is imported anywhere.
_TMP = Path(tempfile.mkdtemp(prefix="vo-test-"))
os.environ["DATA_DIR"] = str(_TMP)
os.environ["DEMO_PASSWORD"] = ""
os.environ["LLM_MODE"] = "replay"
os.environ["EMAIL_PROVIDER"] = "outbox"

import pytest  # noqa: E402

from app import db, demo  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db():
    db.drop_db()
    demo.boot()
    yield
