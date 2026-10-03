"""Environment configuration and thresholds. Read once at import."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def _str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


@dataclass(frozen=True)
class Config:
    gemini_api_key: str = field(default_factory=lambda: _str("GEMINI_API_KEY") or _str("GOOGLE_API_KEY"))
    model_extract: str = field(default_factory=lambda: _str("MODEL_EXTRACT", "gemini-3.5-flash-lite"))
    model_judge: str = field(default_factory=lambda: _str("MODEL_JUDGE", "gemini-3.5-flash-lite"))
    llm_mode: str = field(default_factory=lambda: _str("LLM_MODE", "replay"))  # live | record | replay
    llm_timeout_seconds: int = field(default_factory=lambda: _int("LLM_TIMEOUT_SECONDS", 45))
    search_provider: str = field(default_factory=lambda: _str("SEARCH_PROVIDER", "gemini"))  # gemini | fixture
    daily_llm_call_cap: int = field(default_factory=lambda: _int("DAILY_LLM_CALL_CAP", 500))

    email_provider: str = field(default_factory=lambda: _str("EMAIL_PROVIDER", "outbox"))  # outbox | resend
    resend_api_key: str = field(default_factory=lambda: _str("RESEND_API_KEY"))
    email_from: str = field(default_factory=lambda: _str("EMAIL_FROM", "Vendor Onboarding <onboarding@resend.dev>"))
    email_redirect_to: str = field(default_factory=lambda: _str("EMAIL_REDIRECT_TO"))

    data_dir: Path = field(default_factory=lambda: Path(_str("DATA_DIR", str(ROOT / "data"))))
    demo_password: str = field(default_factory=lambda: _str("DEMO_PASSWORD"))
    session_secret: str = field(default_factory=lambda: _str("SESSION_SECRET"))

    name_fuzzy_ambiguous_low: int = field(default_factory=lambda: _int("NAME_FUZZY_AMBIGUOUS_LOW", 80))
    name_fuzzy_ambiguous_high: int = field(default_factory=lambda: _int("NAME_FUZZY_AMBIGUOUS_HIGH", 95))
    sanctions_fuzzy_threshold: int = field(default_factory=lambda: _int("SANCTIONS_FUZZY_THRESHOLD", 88))
    risk_points_review_threshold: int = field(default_factory=lambda: _int("RISK_POINTS_REVIEW_THRESHOLD", 5))
    bank_letter_max_age_days: int = field(default_factory=lambda: _int("BANK_LETTER_MAX_AGE_DAYS", 90))
    vendor_response_due_days: int = field(default_factory=lambda: _int("VENDOR_RESPONSE_DUE_DAYS", 10))
    auto_close_days: int = field(default_factory=lambda: _int("AUTO_CLOSE_DAYS", 14))
    rule_reverify_days: int = field(default_factory=lambda: _int("RULE_REVERIFY_DAYS", 90))
    rule_promote_after_n_approved: int = field(default_factory=lambda: _int("RULE_PROMOTE_AFTER_N_APPROVED", 3))
    max_upload_mb: int = field(default_factory=lambda: _int("MAX_UPLOAD_MB", 10))
    max_pages: int = 10
    submissions_per_ip_per_hour: int = field(default_factory=lambda: _int("SUBMISSIONS_PER_IP_PER_HOUR", 60))

    @property
    def db_path(self) -> Path:
        return self.data_dir / "app.db"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    def thresholds(self) -> dict[str, int]:
        """Read-only view for the Settings page."""
        return {
            "NAME_FUZZY_AMBIGUOUS_LOW": self.name_fuzzy_ambiguous_low,
            "NAME_FUZZY_AMBIGUOUS_HIGH": self.name_fuzzy_ambiguous_high,
            "SANCTIONS_FUZZY_THRESHOLD": self.sanctions_fuzzy_threshold,
            "RISK_POINTS_REVIEW_THRESHOLD": self.risk_points_review_threshold,
            "BANK_LETTER_MAX_AGE_DAYS": self.bank_letter_max_age_days,
            "VENDOR_RESPONSE_DUE_DAYS": self.vendor_response_due_days,
            "AUTO_CLOSE_DAYS": self.auto_close_days,
            "RULE_REVERIFY_DAYS": self.rule_reverify_days,
            "RULE_PROMOTE_AFTER_N_APPROVED": self.rule_promote_after_n_approved,
            "MAX_UPLOAD_MB": self.max_upload_mb,
            "DAILY_LLM_CALL_CAP": self.daily_llm_call_cap,
        }


config = Config()
