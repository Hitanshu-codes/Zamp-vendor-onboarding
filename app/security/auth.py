"""Shared-password gate (cookie session) and a small per-IP rate limiter."""
from __future__ import annotations

import hmac
import secrets
import threading
import time
from collections import defaultdict, deque

from itsdangerous import BadSignature, URLSafeTimedSerializer
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from app.config import config

COOKIE = "vo_session"
SESSION_MAX_AGE = 7 * 24 * 3600
OPEN_PATHS = ("/healthz", "/login", "/static/")

_secret = config.session_secret or secrets.token_urlsafe(32)
_serializer = URLSafeTimedSerializer(_secret, salt="vo-auth")


def auth_enabled() -> bool:
    return bool(config.demo_password)


def password_ok(candidate: str) -> bool:
    return hmac.compare_digest(candidate.encode(), config.demo_password.encode())


def make_cookie() -> str:
    return _serializer.dumps({"ok": True})


def cookie_valid(value: str | None) -> bool:
    if not value:
        return False
    try:
        _serializer.loads(value, max_age=SESSION_MAX_AGE)
        return True
    except BadSignature:
        return False


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path
        if not auth_enabled() or path.startswith(OPEN_PATHS) or cookie_valid(request.cookies.get(COOKIE)):
            return await call_next(request)
        if path.startswith("/api/"):
            return Response("Authentication required", status_code=401)
        return RedirectResponse(f"/login?next={path}", status_code=303)


class RateLimiter:
    """Sliding one-hour window per key (client IP)."""

    def __init__(self, limit: int, window_seconds: int = 3600) -> None:
        self.limit = limit
        self.window = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            hits = self._hits[key]
            while hits and now - hits[0] > self.window:
                hits.popleft()
            if len(hits) >= self.limit:
                return False
            hits.append(now)
            return True


submission_limiter = RateLimiter(config.submissions_per_ip_per_hour)
