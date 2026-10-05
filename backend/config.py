"""Environment loading and app-wide constants.

Every tunable the spec calls out (staleness thresholds, email start date,
model name) lives here so nothing is hardcoded deeper in the stack.
"""

from __future__ import annotations

from datetime import date
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Scopes required by the spec (section 3.2). Google returns these back on the
# token response; the openid/userinfo pair is what lets us key tokens by email.
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/userinfo.email",
    "openid",
]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Anthropic ---
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-haiku-4-5-20251001"

    # --- Google OAuth ---
    google_credentials_file: Path = PROJECT_ROOT / "credentials.json"
    oauth_redirect_uri: str = "http://localhost:8000/auth/callback"

    # --- App ---
    app_base_url: str = "http://localhost:8000"
    session_secret: str = ""
    session_cookie_name: str = "jobtracker_session"
    session_max_age_days: int = 30
    data_dir: Path = PROJECT_ROOT / "data"

    # --- Gmail scan window (spec 3.3: fixed start of application season) ---
    email_since: date = date(2026, 8, 25)

    # Gate LLM calls on a keyword job-signal check. Mail from a known ATS always
    # bypasses the gate, so the usual application traffic is never affected.
    # Trades a little recall for roughly a 4x cut in extraction cost per rescan.
    use_keyword_prefilter: bool = True

    # --- Staleness thresholds in days (spec 3.6) ---
    stale_days_post_interview: int = 7
    stale_days_post_applied: int = 28

    # --- Sheet ---
    sheet_title: str = "Job Application Tracker"

    # --- Extraction ---
    low_confidence_threshold: float = Field(default=0.7, ge=0.0, le=1.0)

    @property
    def token_store_path(self) -> Path:
        return self.data_dir / "tokens.json"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    return settings
