"""Config loader — reads .env via pydantic-settings, exposes typed Settings.

Wave 1 (P&C insurance digest). Macro-specific settings (FRED, Gmail, Yahoo,
CBOE/CFTC thresholds) stripped — those ingestors are not part of the P&C
universe. Add back per-feature when Wave 2/3 features land.
"""
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Database — separate from macro digest's DB; defaults to project-local file
    db_path: Path = Field(default=Path("./data/state.db"), alias="DB_PATH")

    # Logging
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    # Reddit
    reddit_client_id: str = Field(default="", alias="REDDIT_CLIENT_ID")
    reddit_client_secret: str = Field(default="", alias="REDDIT_CLIENT_SECRET")
    reddit_user_agent: str = Field(default="pc-insurance-digest/0.1", alias="REDDIT_USER_AGENT")

    # EDGAR
    edgar_user_agent: str = Field(default="", alias="EDGAR_USER_AGENT")

    # Summarizer
    summarizer_backend: str = Field(default="mlx_local", alias="SUMMARIZER_BACKEND")
    summarizer_model: str = Field(default="mlx-community/Qwen3.5-27B-4bit", alias="SUMMARIZER_MODEL")
    summarizer_max_per_run: int = Field(default=50, alias="SUMMARIZER_MAX_PER_RUN")
    summarizer_max_per_source: int = Field(default=12, alias="SUMMARIZER_MAX_PER_SOURCE")
    summarizer_timeout_sec: int = Field(default=120, alias="SUMMARIZER_TIMEOUT_SEC")

    # Optional API keys for fallback summarizer backends
    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")
    gemini_api_key: str = Field(default="", alias="GEMINI_API_KEY")

    # Triage (Ollama Qwen2.5:14b — shared local server with macro digest)
    ollama_host: str = Field(default="http://localhost:11434", alias="OLLAMA_HOST")
    ollama_model: str = Field(default="qwen2.5:14b", alias="OLLAMA_MODEL")
    triage_min_score: float = Field(default=0.5, alias="TRIAGE_MIN_SCORE")
    triage_lookback_hours: int = Field(default=24, alias="TRIAGE_LOOKBACK_HOURS")

    # Obsidian — vault is shared with macro digest; we land in a sibling folder
    obsidian_vault_path: str = Field(default="", alias="OBSIDIAN_VAULT_PATH")
    obsidian_digest_dir: str = Field(default="81 P&C Digest", alias="OBSIDIAN_DIGEST_DIR")

    # MLX-LM local server (Apple Silicon, shared with macro digest)
    mlx_server_url: str = Field(default="http://localhost:8080", alias="MLX_SERVER_URL")
    mlx_model: str = Field(default="mlx-community/Qwen3.5-27B-4bit", alias="MLX_MODEL")

    # HN threshold
    hn_min_points: int = Field(default=100, alias="HN_MIN_POINTS")

    # FRED — P&C loss-cost driver CPI/PPI series. Same key as macro-ai-digest;
    # free tier is generous (120 req/min default).
    fred_api_key: str = Field(default="", alias="FRED_API_KEY")
    # z-score threshold over trailing 12 months — emit only anomalous prints
    fred_zscore_threshold: float = Field(default=1.5, alias="FRED_ZSCORE_THRESHOLD")

    # CourtListener — free PACER mirror. Get a token at https://www.courtlistener.com/help/api/
    # Free tier limits: 5 req/min, 50 req/hour, 125 req/day.
    courtlistener_token: str = Field(default="", alias="COURTLISTENER_TOKEN")

    # Databricks medallion sink (Wave 3 Phase 1 scaffold). All writes no-op when
    # databricks_enabled=False; workspace provisioning is deferred to Wave 4.
    # See packages/digest-core/sql/databricks/{bronze,silver,gold}.sql for DDL.
    databricks_enabled: bool = Field(default=False, alias="DATABRICKS_ENABLED")
    databricks_host: str = Field(default="", alias="DATABRICKS_HOST")          # workspace URL, no scheme
    databricks_http_path: str = Field(default="", alias="DATABRICKS_HTTP_PATH") # SQL warehouse HTTP path
    databricks_token: str = Field(default="", alias="DATABRICKS_TOKEN")        # personal access token
    databricks_catalog: str = Field(default="pc_digest", alias="DATABRICKS_CATALOG")

    # ── Validators ────────────────────────────────────────────────────────

    @field_validator("summarizer_model", mode="before")
    @classmethod
    def _validate_model_name(cls, v: str) -> str:
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9\-._/]*", str(v)):
            raise ValueError(
                f"SUMMARIZER_MODEL must contain only letters, digits, hyphens, dots, "
                f"underscores, or slashes — got: {v!r}"
            )
        return v

    @field_validator("ollama_host", "mlx_server_url", mode="before")
    @classmethod
    def _validate_localhost_url(cls, v: str) -> str:
        hostname = urlparse(str(v)).hostname
        if hostname not in ("localhost", "127.0.0.1", "::1"):
            raise ValueError(
                f"URL must point to localhost for safety, got hostname: {hostname!r}"
            )
        return v


settings = Settings()
