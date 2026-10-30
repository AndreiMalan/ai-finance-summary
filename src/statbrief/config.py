"""Application settings loaded from environment variables (and `.env` locally)."""
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Upper bound (seconds) for one /_diag/sse stream: below Vercel's 300 s function maxDuration
# (Hobby maximum, vercel.json) with a 10 s margin for cold start and response overhead.
SSE_DIAG_MAX_TOTAL_S = 290.0
# Vercel function maxDuration (vercel.json, Hobby maximum); one graph segment must finish well inside it.
FUNCTION_MAX_DURATION_S = 300.0
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

    # Public statistics APIs (M3). Base URLs must be https: their responses become report data.
    eurostat_base_url: str = "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data"
    worldbank_base_url: str = "https://api.worldbank.org/v2"
    api_timeout_s: float = Field(20.0, gt=0)  # per HTTP attempt
    api_retries: int = Field(3, ge=0, le=10)  # extra attempts after 429/5xx/timeouts/invalid JSON
    api_backoff_s: float = Field(1.0, ge=0)  # first retry delay; doubles on every retry
    api_max_response_mb: int = Field(20, ge=1)  # larger responses are refused (memory guard)
    api_total_budget_s: float = Field(60.0, gt=0)  # wall-clock cap per fetch, across retries and pages
    worldbank_per_page: int = Field(1000, ge=1, le=20000)  # rows per World Bank page
    worldbank_max_pages: int = Field(20, ge=1)  # more pages than this → error (no unbounded pagination)
    conflict_rel_tol: float = Field(0.005, ge=0.0, le=0.5)  # D-010: relative tolerance before rounding units
    enable_vector_search: bool = True  # D-011 toggle; False (or no embeddings) -> Postgres full-text search
    embeddings_provider: Literal["openai"] = "openai"
    embeddings_model: str = "text-embedding-3-small"
    embedding_dimensions: int = Field(1536, ge=1, le=PGVECTOR_HNSW_MAX_DIMENSIONS)  # must match kb_chunks
    openai_api_key: SecretStr | None = None
    rag_top_k: int = Field(6, ge=1, le=50)  # passages returned per retrieval (default k)
    rag_max_k: int = Field(50, ge=1, le=200)  # upper bound for any caller-supplied k
    rag_chunk_size: int = Field(1200, ge=100)  # characters per chunk (RecursiveCharacterTextSplitter)
    rag_chunk_overlap: int = Field(150, ge=0)  # characters shared by consecutive chunks
    max_reference_mb: int = Field(10, ge=1)  # size cap for user-uploaded reference documents
    max_reference_pages: int = Field(300, ge=1)  # PDF page cap (bounds text-extraction work)
    significance_rate_pp: float = Field(1.0, gt=0)  # |change| in pp that flags a rate (D-010, AC 25)
    significance_level_pct: float = Field(2.0, gt=0)  # |% change| that flags a level (D-010, AC 25)
    top_movers: int = Field(3, ge=0)  # largest |YoY| per indicator and period across >= 2 regions (0 = off)
    max_revisions: int = Field(3, ge=0)  # verify -> revise cycles before safe failure (D-010, AC 27)
    max_parse_retries: int = Field(2, ge=0)  # retries of an unparseable LLM draft (D-010)
    max_model_retries: int = Field(1, ge=0)  # node-level retries of a failed model call (after LLM_MAX_RETRIES)

    # LLM layer (M6). The chat model is built with init_chat_model(f"{llm_provider}:{llm_model}").
    llm_provider: Literal["openai"] = "openai"
    llm_model: str = Field("gpt-5-mini", min_length=1)
    # Ignored by langchain-openai for reasoning models (gpt-5, gpt-5-mini: only the default 1 is accepted).
    llm_temperature: float = Field(0.2, ge=0.0, le=2.0)
    llm_timeout_s: float = Field(60.0, gt=0)  # per model request
    llm_max_retries: int = Field(2, ge=0, le=10)  # provider-level retries of a failed request
    # with_structured_output method: "function_calling" (tool call, lenient schema) or "json_schema".
    llm_structured_output_method: Literal["function_calling", "json_schema"] = "function_calling"
    llm_max_tool_steps: int = Field(6, ge=1, le=50)  # model turns in one tool-calling loop
    max_tool_rows: int = Field(200, ge=1)  # rows one query_observations tool call may return
    max_tool_rows_per_turn: int = Field(400, ge=1)  # rows all query_observations calls of one tool loop may return
    prompt_max_metrics: int = Field(80, ge=1)  # metrics listed in a prompt (significant ones first)
    prompt_max_passages: int = Field(8, ge=1)  # retrieved passages included in a prompt
    prompt_max_skips: int = Field(40, ge=0)  # "not computed" metric lines in a prompt (reasons for gaps)
    log_prompts: bool = False  # debug: store prompt/response text in run_events (owner-only diagnostics)

    # LangGraph workflow (M7, D-011 toggles). The core path works with every ENABLE_* toggle off (AC 46).
    enable_parallel: bool = True  # Send fan-out per source / per region x indicator; off -> one sequential node
    enable_replay: bool = True  # history, replay and fork from checkpoints (M7b)
    enable_draft_tools: bool = True  # let the model call the controlled tools while drafting (when it supports tools)
    segment_budget_s: float = Field(240.0, gt=0)  # wall-clock budget of one run segment (< FUNCTION_MAX_DURATION_S)
    graph_recursion_limit: int = Field(60, ge=10)  # LangGraph super-steps per invocation

    cron_secret: SecretStr = SecretStr("")

    sse_diag_ticks: int = Field(5, ge=1, le=60)
    sse_diag_interval_s: float = Field(1.0, ge=0.0, le=10.0)
    enable_diag_endpoints: bool = False  # /_diag/* outside dev/test only when true

    # Exports (M9). Presentation only: stored and exported values are never rounded.
    export_default_decimals: int = Field(2, ge=0, le=6)  # decimals for a number that no claim text states
    # lines per PDF chart (series with most points first); at most the 6 distinct colours of the PDF palette
    export_chart_max_series: int = Field(6, ge=1, le=6)

    # Retention (M9, D-009 / AC 50). Every period is env-configurable.
    retention_unfinished_days: int = Field(30, ge=1)  # raw files of unapproved reports, after last activity
    retention_approved_days: int = Field(7, ge=1)  # raw files of approved reports, after approval
    checkpoint_retention_days: int = Field(30, ge=1)  # LangGraph threads, after the report's last activity
    log_retention_days: int = Field(14, ge=1)  # run_events, login_attempts, usage_events
    upload_abandon_hours: int = Field(24, ge=1)  # staged chunked uploads never completed
    blob_orphan_grace_hours: int = Field(24, ge=1)  # unreferenced blobs older than this are swept
    retention_batch_size: int = Field(500, ge=1)  # rows (and blobs) handled per rule per cleanup run

    @field_validator("session_secret")
    @classmethod
    def _secret_long_enough(cls, v: SecretStr) -> SecretStr:
        """Reject session secrets shorter than 32 characters."""
        if len(v.get_secret_value()) < 32:
            raise ValueError("SESSION_SECRET must be at least 32 characters")
        return v

    @field_validator("eurostat_base_url", "worldbank_base_url")
    @classmethod
    def _https_base_url(cls, v: str) -> str:
        """Require an https:// base URL without a trailing slash."""
        if not v.startswith("https://"):
            raise ValueError("API base URLs must start with https://")
        return v.rstrip("/")

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
    def _top_k_within_max_k(self) -> "Settings":
        """Reject a default k above the k cap."""
        if self.rag_top_k > self.rag_max_k:
            raise ValueError("RAG_TOP_K must not exceed RAG_MAX_K")
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

    @model_validator(mode="after")
    def _segment_within_max_duration(self) -> "Settings":
        """Reject a run-segment budget that would not end before the serverless function is killed."""
        if self.segment_budget_s >= FUNCTION_MAX_DURATION_S:
            raise ValueError(f"SEGMENT_BUDGET_S must be below {FUNCTION_MAX_DURATION_S:g} s")
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
