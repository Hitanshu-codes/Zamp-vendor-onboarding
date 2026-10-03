import os
import tempfile
from pathlib import Path

# Must be set before app.config is imported anywhere.
_TMP = Path(tempfile.mkdtemp(prefix="vo-test-"))
os.environ["DATA_DIR"] = str(_TMP)
os.environ["DEMO_PASSWORD"] = ""
os.environ["LLM_MODE"] = "replay"
os.environ["EMAIL_PROVIDER"] = "outbox"
# Tests never make paid API calls: blank keys win over .env (python-dotenv does not override set vars).
os.environ["GEMINI_API_KEY"] = ""
os.environ["GOOGLE_API_KEY"] = ""
os.environ["RESEND_API_KEY"] = ""

import pytest  # noqa: E402

from app import db, demo  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db():
    db.drop_db()
    demo.boot()
    yield
