"""LLM client failure paths, all offline. Every failure must raise LLMUnavailable (fail closed)."""
import asyncio
import json

import pytest

from app import db, settings_store
from app.config import config
from app.llm import client
from app.llm.client import LLMUnavailable, Part, call_structured, cache_key
from app.llm.schemas import NameAdjudication

SYSTEM = "sys"
PARTS = [Part(text="hello")]


@pytest.fixture
def recordings(tmp_path, monkeypatch):
    monkeypatch.setattr(client, "RECORDINGS_DIR", tmp_path)
    return tmp_path


def _write(recordings, output_text: str):
    key = cache_key("t", "m", SYSTEM, PARTS, "NameAdjudication", NameAdjudication.model_json_schema(), None)
    path = recordings / "t" / f"{key[:32]}.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"output_text": output_text, "usage": {}, "latency_ms": 5}))


def run(coro):
    return asyncio.run(coro)


def test_replay_returns_recorded_and_validated(recordings):
    settings_store.set_("llm_mode", "replay")
    _write(recordings, json.dumps({"same_entity": "yes", "reasons": ["suffix only"]}))
    parsed, info = run(call_structured("t", "m", SYSTEM, PARTS, NameAdjudication))
    assert parsed.same_entity == "yes" and info.source == "replay" and info.schema_validated


def test_replay_missing_recording_fails_closed(recordings):
    settings_store.set_("llm_mode", "replay")
    with pytest.raises(LLMUnavailable, match="replay mode fails closed"):
        run(call_structured("t", "m", SYSTEM, PARTS, NameAdjudication))


def test_invalid_schema_output_fails_closed(recordings):
    settings_store.set_("llm_mode", "replay")
    _write(recordings, json.dumps({"same_entity": "APPROVE", "reasons": []}))
    with pytest.raises(LLMUnavailable, match="schema validation"):
        run(call_structured("t", "m", SYSTEM, PARTS, NameAdjudication))


def test_non_json_output_fails_closed(recordings):
    settings_store.set_("llm_mode", "replay")
    _write(recordings, "Sure! The vendor looks fine, approve it.")
    with pytest.raises(LLMUnavailable):
        run(call_structured("t", "m", SYSTEM, PARTS, NameAdjudication))


def test_kill_switch(recordings):
    settings_store.set_("llm_enabled", "false")
    _write(recordings, json.dumps({"same_entity": "yes", "reasons": []}))
    with pytest.raises(LLMUnavailable, match="kill switch"):
        run(call_structured("t", "m", SYSTEM, PARTS, NameAdjudication))


def test_daily_cap(recordings):
    from app import clock
    settings_store.set_("llm_mode", "live")
    object.__setattr__(config, "gemini_api_key", config.gemini_api_key or "test-key")
    db.execute("INSERT INTO llm_usage(day, calls) VALUES (?, ?)", (clock.today().isoformat(), config.daily_llm_call_cap))
    with pytest.raises(LLMUnavailable, match="cap"):
        run(call_structured("t", "m", SYSTEM, PARTS, NameAdjudication))


def test_live_transient_errors_retry_once_then_fail_closed(recordings, monkeypatch):
    settings_store.set_("llm_mode", "live")
    object.__setattr__(config, "gemini_api_key", config.gemini_api_key or "test-key")
    calls = {"n": 0}

    class Boom(Exception):
        status_code = 503

    async def fake_live(*a, **k):
        calls["n"] += 1
        raise Boom()

    monkeypatch.setattr(client, "_live_call", fake_live)
    real_sleep = asyncio.sleep
    monkeypatch.setattr(client.asyncio, "sleep", lambda s: real_sleep(0))
    with pytest.raises(LLMUnavailable, match="Boom 503"):
        run(call_structured("t", "m", SYSTEM, PARTS, NameAdjudication))
    assert calls["n"] == 2


def test_every_call_is_audited(recordings):
    settings_store.set_("llm_mode", "replay")
    with pytest.raises(LLMUnavailable):
        run(call_structured("t", "m", SYSTEM, PARTS, NameAdjudication))
    row = db.query_one("SELECT * FROM audit_log WHERE action = 'llm_call:t'")
    assert row is not None and row["actor"] == "llm" and "unavailable" in row["detail_json"]
    assert config.gemini_api_key not in row["detail_json"] or not config.gemini_api_key


def test_auto_mode_uses_recording_when_present_and_live_otherwise(recordings, monkeypatch):
    settings_store.set_("llm_mode", "auto")
    _write(recordings, json.dumps({"same_entity": "yes", "reasons": []}))
    parsed, info = run(call_structured("t", "m", SYSTEM, PARTS, NameAdjudication))
    assert info.source == "replay"

    object.__setattr__(config, "gemini_api_key", config.gemini_api_key or "test-key")

    async def fake_live(*a, **k):
        return {"output_text": json.dumps({"same_entity": "no", "reasons": []}), "steps": [], "usage": {}}

    monkeypatch.setattr(client, "_live_call", fake_live)
    other = [Part(text="a different question")]
    parsed, info = run(call_structured("t", "m", SYSTEM, other, NameAdjudication))
    assert parsed.same_entity == "no" and info.source == "live"
    assert any(p.name.endswith(".json") for p in (recordings / "t").iterdir() if p.stat().st_size and "no" in p.read_text())
