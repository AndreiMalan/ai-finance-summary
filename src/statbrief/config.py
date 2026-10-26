"""Application settings loaded from environment variables (and `.env` locally)."""
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Upper bound (seconds) for one /_diag/sse stream: below Vercel's 300 s function maxDuration
# (Hobby maximum, vercel.json) with a 10 s margin for cold start and response overhead.
SSE_DIAG_MAX_TOTAL_S = 290.0
# pgvector's HNSW index supports at most 2000 dimensions for the `vector` type.
PGVECTOR_HNSW_MAX_DIMENSIONS = 2000


class Settings(BaseSettings):
    """Every tunable of the app. Field names map to UPPER_CASE env vars."""

    # hide_input_in_errors: a ValidationError must never echo SESSION_SECRET or a DB password.
    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", case_sensitive=False, env_ignore_empty=True,
        hide_input_in_errors=True,
    )

    app_env: Literal["dev", "test", "prod"] = "dev"
    database_url: str
    database_url_unpooled: str | None = None
    session_secret: SecretStr
    db_pool_min: int = Field(1, ge=0)
    db_pool_max: int = Field(5, ge=1)
    db_setup_lock_timeout_s: float = Field(60.0, gt=0)
    db_connect_timeout_s: float = Field(5.0, gt=0)  # libpq connect_timeout (rounded up to whole s)
    db_pool_open_timeout_s: float = Field(10.0, gt=0)  # max wait for the pool's first connections
    healthz_db_timeout_s: float = Field(3.0, gt=0)

    signups_enabled: bool = True
    demo_user_email: str | None = None
    demo_user_password: SecretStr | None = None
    password_min_length: int = Field(10, ge=8)
    password_max_length: int = Field(128, ge=16)
    session_max_age_days: int = Field(30, ge=1)
    session_short_max_age_hours: int = Field(12, ge=1)
    login_max_failures: int = Field(5, ge=1)
    login_window_minutes: int = Field(15, ge=1)

    runs_per_hour: int = Field(10, ge=1)
    daily_llm_call_cap: int = Field(500, ge=1)
    max_upload_mb: int = Field(10, ge=1)
    max_files_per_report: int = Field(5, ge=1)
    upload_chunk_mb: float = Field(4.0, gt=0, le=4.4)  # below Vercel's 4.5 MB request body limit (D-021)
    max_uncompressed_ratio: int = Field(100, ge=2)  # zip-bomb guard for .xlsx
    max_uncompressed_mb: int = Field(200, ge=1)  # absolute expansion cap for .xlsx
    max_rows_per_file: int = Field(200_000, ge=1)  # non-empty rows kept per table
    max_scan_rows_per_file: int = Field(1_000_000, ge=1)  # rows scanned incl. empty ones (gap-row DoS guard)
    max_columns_per_file: int = Field(500, ge=1)  # widest accepted row (memory guard)
    preview_rows: int = Field(50, ge=1, le=500)  # data rows returned by the file preview

    blob_backend: Literal["local", "vercel"] = "local"  # "vercel" = private Vercel Blob store (D-021)
    local_blob_dir: str = ".local_blobs"  # root directory of the local blob store (dev/tests)

    detect_sample_rows: int = Field(200, ge=10)  # rows sampled per column for detection
    detect_header_scan_rows: int = Field(20, ge=1)  # grid rows scanned to find the header
    mapping_min_confidence: float = Field(0.8, ge=0.0, le=1.0)  # below -> user must confirm the mapping

    enable_vector_search: bool = True  # D-011 toggle; False (or no embeddings) -> Postgres full-text search
    embeddings_provider: Literal["openai"] = "openai"
    embeddings_model: str = "text-embedding-3-small"
    embedding_dimensions: int = Field(1536, ge=1, le=PGVECTOR_HNSW_MAX_DIMENSIONS)  # must match kb_chunks
    openai_api_key: SecretStr | None = None
    rag_top_k: int = Field(6, ge=1, le=50)  # passages returned per retrieval
    rag_chunk_size: int = Field(1200, ge=100)  # characters per chunk (RecursiveCharacterTextSplitter)
    rag_chunk_overlap: int = Field(150, ge=0)  # characters shared by consecutive chunks
    max_reference_mb: int = Field(10, ge=1)  # size cap for user-uploaded reference documents
    max_reference_pages: int = Field(300, ge=1)  # PDF page cap (bounds text-extraction work)

    cron_secret: SecretStr = SecretStr("")

    sse_diag_ticks: int = Field(5, ge=1, le=60)
    sse_diag_interval_s: float = Field(1.0, ge=0.0, le=10.0)
    enable_diag_endpoints: bool = False  # /_diag/* outside dev/test only when true

    @field_validator("session_secret")
    @classmethod
    def _secret_long_enough(cls, v: SecretStr) -> SecretStr:
        """Reject session secrets shorter than 32 characters."""
        if len(v.get_secret_value()) < 32:
            raise ValueError("SESSION_SECRET must be at least 32 characters")
        return v

    @model_validator(mode="after")
    def _pool_bounds_consistent(self) -> "Settings":
        """Reject a pool minimum larger than the pool maximum."""
        if self.db_pool_min > self.db_pool_max:
            raise ValueError("DB_POOL_MIN must not exceed DB_POOL_MAX")
        return self

    @model_validator(mode="after")
    def _password_bounds_consistent(self) -> "Settings":
        """Reject a password minimum length larger than the maximum length."""
        if self.password_min_length > self.password_max_length:
            raise ValueError("PASSWORD_MIN_LENGTH must not exceed PASSWORD_MAX_LENGTH")
        return self

    @model_validator(mode="after")
    def _short_session_below_long_session(self) -> "Settings":
        """Reject a non-remember session lifetime that is not shorter than the remember-me lifetime."""
        if self.session_short_max_age_hours >= self.session_max_age_days * 24:
            raise ValueError("SESSION_SHORT_MAX_AGE_HOURS must be below SESSION_MAX_AGE_DAYS * 24")
        return self

    @model_validator(mode="after")
    def _chunk_overlap_below_size(self) -> "Settings":
        """Reject a chunk overlap that is not smaller than the chunk size (the splitter would not advance)."""
        if self.rag_chunk_overlap >= self.rag_chunk_size:
            raise ValueError("RAG_CHUNK_OVERLAP must be below RAG_CHUNK_SIZE")
        return self

    @model_validator(mode="after")
    def _prod_requires_vercel_blob(self) -> "Settings":
        """Reject local-disk blob storage in prod: uploads must go to the private Vercel Blob store (D-021)."""
        if self.app_env == "prod" and self.blob_backend != "vercel":
            raise ValueError("BLOB_BACKEND must be 'vercel' in production")
        return self

    @model_validator(mode="after")
    def _sse_diag_within_max_duration(self) -> "Settings":
        """Reject SSE diagnostics streams that could outlive the serverless function."""
        if self.sse_diag_ticks * self.sse_diag_interval_s >= SSE_DIAG_MAX_TOTAL_S:
            raise ValueError(
                f"SSE_DIAG_TICKS * SSE_DIAG_INTERVAL_S must be below {SSE_DIAG_MAX_TOTAL_S:g} s"
            )
        return self

    @property
    def diag_endpoints_enabled(self) -> bool:
        """Whether /_diag/* routes respond: always outside prod, in prod only when enabled."""
        return self.app_env != "prod" or self.enable_diag_endpoints

    @property
    def bootstrap_database_url(self) -> str:
        """Direct (unpooled) URL for migrations and LangGraph setup, else the pooled URL."""
        return self.database_url_unpooled or self.database_url

    @property
    def cookie_secure(self) -> bool:
        """Whether cookies carry the Secure flag (HTTPS only)."""
        return self.app_env == "prod"


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings instance (read once from the environment)."""
    return Settings()  # type: ignore[call-arg]
