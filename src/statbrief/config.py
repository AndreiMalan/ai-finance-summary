"""Application settings loaded from environment variables (and `.env` locally)."""
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Every tunable of the app. Field names map to UPPER_CASE env vars."""

    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", case_sensitive=False, env_ignore_empty=True
    )

    app_env: Literal["dev", "test", "prod"] = "dev"
    database_url: str
    session_secret: SecretStr
    db_pool_max: int = Field(5, ge=1)

    signups_enabled: bool = True
    demo_user_email: str | None = None
    demo_user_password: SecretStr | None = None
    password_min_length: int = Field(10, ge=8)
    session_max_age_days: int = Field(30, ge=1)
    login_max_failures: int = Field(5, ge=1)
    login_window_minutes: int = Field(15, ge=1)

    runs_per_hour: int = Field(10, ge=1)
    daily_llm_call_cap: int = Field(500, ge=1)
    max_upload_mb: int = Field(10, ge=1)
    max_files_per_report: int = Field(5, ge=1)

    cron_secret: SecretStr = SecretStr("")

    @field_validator("session_secret")
    @classmethod
    def _secret_long_enough(cls, v: SecretStr) -> SecretStr:
        """Reject session secrets shorter than 32 characters."""
        if len(v.get_secret_value()) < 32:
            raise ValueError("SESSION_SECRET must be at least 32 characters")
        return v

    @property
    def cookie_secure(self) -> bool:
        """Whether cookies carry the Secure flag (HTTPS only)."""
        return self.app_env == "prod"


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings instance (read once from the environment)."""
    return Settings()  # type: ignore[call-arg]
