"""The run currently executing on this task, so cross-cutting logs (LLM calls) can be tied to it."""
from contextvars import ContextVar

current_run_id: ContextVar[int | None] = ContextVar("current_run_id", default=None)
