"""The single wrapper for every LLM call.

- Structured output: the response must match a pydantic schema, and it is re-validated here.
- Fail closed: kill switch, missing key, cap reached, timeout, API error, refusal, empty or
  invalid output all raise LLMUnavailable. Callers turn that into an LLM_UNAVAILABLE check.
- Modes: live (real calls), record (live + save), replay (saved responses only; missing = unavailable).
- Every call is written to the audit log (task, model, input hashes, truncated output, latency, tokens).
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from app import audit, clock, db, settings_store
from app.config import ROOT, config

RECORDINGS_DIR = ROOT / "recordings"
TRANSIENT_STATUS = {408, 409, 429, 500, 502, 503, 504}
T = TypeVar("T", bound=BaseModel)


class LLMUnavailable(Exception):
    def __init__(self, reason: str, task: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.task = task


@dataclass
class Part:
    """One piece of model input: text, or a file (PDF/image bytes)."""

    text: str | None = None
    data: bytes | None = None
    mime_type: str | None = None

    def fingerprint(self) -> str:
        if self.data is not None:
            return f"file:{self.mime_type}:{hashlib.sha256(self.data).hexdigest()}"
        return f"text:{self.text}"

    def to_api(self) -> dict[str, Any]:
        if self.data is not None:
            kind = "document" if self.mime_type == "application/pdf" else "image"
            return {"type": kind, "data": base64.b64encode(self.data).decode(), "mime_type": self.mime_type}
        return {"type": "text", "text": self.text or ""}


@dataclass
class CallInfo:
    task: str
    model: str
    mode: str
    cache_key: str
    latency_ms: int = 0
    tokens: dict[str, int] = field(default_factory=dict)
    source: str = "live"  # live | replay
    schema_validated: bool = False

    def evidence(self) -> dict[str, Any]:
        return {"llm_task": self.task, "llm_model": self.model, "llm_mode": self.mode, "llm_latency_ms": self.latency_ms,
                "llm_source": self.source, "schema_validated": self.schema_validated, "llm_tokens": self.tokens}


def _schema_hash(schema: dict | None) -> str:
    return hashlib.sha256(json.dumps(schema or {}, sort_keys=True).encode()).hexdigest()[:12]


def cache_key(task: str, model: str, system: str, parts: list[Part], schema_name: str, schema: dict | None, tools: list | None) -> str:
    h = hashlib.sha256()
    for piece in (task, model, system, schema_name, _schema_hash(schema), json.dumps(tools or [], sort_keys=True)):
        h.update(piece.encode())
        h.update(b"\x00")
    for p in parts:
        h.update(p.fingerprint().encode())
        h.update(b"\x00")
    return h.hexdigest()


def _recording_path(task: str, key: str) -> Path:
    return RECORDINGS_DIR / task / f"{key[:32]}.json"


def _count_call() -> None:
    """Enforce DAILY_LLM_CALL_CAP (protects the API budget on the public demo)."""
    day = clock.today().isoformat()
    with db.connect() as conn:
        row = conn.execute("SELECT calls FROM llm_usage WHERE day = ?", (day,)).fetchone()
        calls = row["calls"] if row else 0
        if calls >= config.daily_llm_call_cap:
            raise LLMUnavailable(f"daily LLM call cap ({config.daily_llm_call_cap}) reached")
        conn.execute("INSERT INTO llm_usage(day, calls) VALUES (?, 1) ON CONFLICT(day) DO UPDATE SET calls = calls + 1", (day,))


_client = None


def _get_client():
    global _client
    if _client is None:
        from google import genai
        _client = genai.Client(api_key=config.gemini_api_key)
    return _client


async def _live_call(model: str, system: str, parts: list[Part], response_schema: dict | None, tools: list | None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": model,
        "input": [p.to_api() for p in parts],
        "system_instruction": system,
        "generation_config": {"thinking_level": "low", "temperature": 0},
        "store": False,
    }
    if response_schema is not None:
        body["response_format"] = {"type": "text", "mime_type": "application/json", "schema": response_schema}
    if tools:
        body["tools"] = tools
    interaction = await _get_client().aio.interactions.create(**body)
    dumped = interaction.model_dump(mode="json", exclude_none=True)
    if dumped.get("status") not in (None, "completed"):
        raise LLMUnavailable(f"interaction status {dumped.get('status')}")
    usage = dumped.get("usage") or {}
    return {
        "output_text": interaction.output_text,
        "steps": dumped.get("steps", []),
        "usage": {k: usage.get(k, 0) for k in ("total_input_tokens", "total_output_tokens", "total_thought_tokens", "total_tool_use_tokens")},
    }


async def _invoke(task: str, model: str, system: str, parts: list[Part], schema_name: str,
                  response_schema: dict | None, tools: list | None) -> tuple[dict[str, Any], CallInfo]:
    mode = settings_store.get("llm_mode")
    key = cache_key(task, model, system, parts, schema_name, response_schema, tools)
    info = CallInfo(task=task, model=model, mode=mode, cache_key=key)

    if not settings_store.get_bool("llm_enabled"):
        raise LLMUnavailable("LLM disabled by the admin kill switch", task)

    path = _recording_path(task, key)
    if mode == "replay":
        if not path.exists():
            raise LLMUnavailable("no recorded response for this exact input (replay mode fails closed)", task)
        raw = json.loads(path.read_text(encoding="utf-8"))
        info.source = "replay"
        info.latency_ms = raw.get("latency_ms", 0)
        info.tokens = raw.get("usage", {})
        return raw, info

    if not config.gemini_api_key:
        raise LLMUnavailable("GEMINI_API_KEY is not configured", task)
    _count_call()

    last_error = "unknown error"
    for attempt in range(2):  # one retry on transient errors
        start = time.perf_counter()
        try:
            raw = await asyncio.wait_for(_live_call(model, system, parts, response_schema, tools), timeout=config.llm_timeout_seconds)
            info.latency_ms = int((time.perf_counter() - start) * 1000)
            info.tokens = raw["usage"]
            raw["latency_ms"] = info.latency_ms
            if mode == "record":
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps({"task": task, "model": model, "schema": schema_name, "key": key, **raw},
                                           indent=2, ensure_ascii=False), encoding="utf-8")
            return raw, info
        except LLMUnavailable:
            raise
        except asyncio.TimeoutError:
            last_error = f"timed out after {config.llm_timeout_seconds}s"
        except Exception as exc:  # SDK errors live in a private module; classify by status code
            status = getattr(exc, "status_code", None)
            last_error = f"{type(exc).__name__}{f' {status}' if status else ''}"
            if status is not None and status not in TRANSIENT_STATUS:
                break
        if attempt == 0:
            await asyncio.sleep(1.0)
    raise LLMUnavailable(f"LLM call failed: {last_error}", task)


def _audit(info: CallInfo, parts: list[Part], outcome: str, output: str | None) -> None:
    audit.log("llm", f"llm_call:{info.task}", "llm", info.cache_key[:16], {
        "model": info.model, "mode": info.mode, "source": info.source, "outcome": outcome,
        "input_hashes": [hashlib.sha256(p.fingerprint().encode()).hexdigest()[:16] for p in parts],
        "output": (output or "")[:500], "latency_ms": info.latency_ms, "tokens": info.tokens,
    })


async def call_structured(task: str, model: str, system: str, parts: list[Part], schema: type[T]) -> tuple[T, CallInfo]:
    """Run a schema-constrained call and return the validated object. Raises LLMUnavailable on any failure."""
    json_schema = schema.model_json_schema()
    info = CallInfo(task=task, model=model, mode=settings_store.get("llm_mode"), cache_key="")
    try:
        raw, info = await _invoke(task, model, system, parts, schema.__name__, json_schema, None)
        text = raw.get("output_text") or ""
        if not text.strip():
            raise LLMUnavailable("empty response (possibly blocked)", task)
        try:
            parsed = schema.model_validate_json(text)
        except ValidationError as exc:
            raise LLMUnavailable(f"response failed schema validation ({exc.error_count()} errors)", task) from exc
        info.schema_validated = True
        _audit(info, parts, "ok", text)
        return parsed, info
    except LLMUnavailable as exc:
        info.cache_key = info.cache_key or cache_key(task, model, system, parts, schema.__name__, json_schema, None)
        _audit(info, parts, f"unavailable: {exc.reason}", None)
        raise


async def call_with_tools(task: str, model: str, system: str, parts: list[Part], tools: list) -> tuple[dict[str, Any], CallInfo]:
    """Unstructured call with server-side tools (used only for tax-rule web search). Returns the raw interaction."""
    info = CallInfo(task=task, model=model, mode=settings_store.get("llm_mode"), cache_key="")
    try:
        raw, info = await _invoke(task, model, system, parts, "raw", None, tools)
        _audit(info, parts, "ok", raw.get("output_text"))
        return raw, info
    except LLMUnavailable as exc:
        info.cache_key = info.cache_key or cache_key(task, model, system, parts, "raw", None, tools)
        _audit(info, parts, f"unavailable: {exc.reason}", None)
        raise
