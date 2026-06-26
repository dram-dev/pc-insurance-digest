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

    # ════════════════════════════════════════════════════════════════════
    # LOCAL LLM MODELS — upgrade here (plug-and-play, no code changes)
    # ════════════════════════════════════════════════════════════════════
    # Every pipeline stage routes through the pluggable backend registry
    # (digest_core.summarize.backends). To move to a newer model, change ONLY
    # the env vars below — `digest models` then shows what's wired and pings
    # each one for reachability. Available backends: claude_cli_pro · haiku_api
    # · gemini_flash_free · local_qwen (Ollama) · mlx_local (MLX server).
    #
    #   stage      backend var           model var
    #   ─────      ───────────           ─────────
    #   triage     TRIAGE_BACKEND        OLLAMA_MODEL / MLX_MODEL (per backend)
    #   summarize  SUMMARIZER_BACKEND    MLX_MODEL / SUMMARIZER_MODEL (per backend)
    #   regime     SUMMARIZER_BACKEND    (shares the summarizer backend)
    #   embeddings (Ollama only)         EMBEDDING_MODEL
    #   weekly     SUMMARIZER_BACKEND    (shares the summarizer backend)

    # Summarizer (long-form notes + regime judgment)
    summarizer_backend: str = Field(default="mlx_local", alias="SUMMARIZER_BACKEND")
    summarizer_model: str = Field(default="mlx-community/Qwen3.5-27B-4bit", alias="SUMMARIZER_MODEL")
    summarizer_max_per_run: int = Field(default=50, alias="SUMMARIZER_MAX_PER_RUN")
    summarizer_max_per_source: int = Field(default=12, alias="SUMMARIZER_MAX_PER_SOURCE")
    summarizer_timeout_sec: int = Field(default=120, alias="SUMMARIZER_TIMEOUT_SEC")

    # Optional API keys for the cloud summarizer backends
    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")
    gemini_api_key: str = Field(default="", alias="GEMINI_API_KEY")

    # Triage (keep/drop + topic). Routes through the backend registry too, so it
    # can run on Ollama (local_qwen, default) or any other backend. The model is
    # the backend's model var (OLLAMA_MODEL for local_qwen, MLX_MODEL for mlx_local).
    triage_backend: str = Field(default="local_qwen", alias="TRIAGE_BACKEND")
    triage_max_tokens: int = Field(default=384, alias="TRIAGE_MAX_TOKENS")
    ollama_host: str = Field(default="http://localhost:11434", alias="OLLAMA_HOST")
    ollama_model: str = Field(default="qwen2.5:14b", alias="OLLAMA_MODEL")
    # Ollama think field: unset → omitted from the request (required for
    # non-thinking models like qwen2.5); false → suppress reasoning on
    # thinking-default models (qwen3.x). Set OLLAMA_THINK=false with qwen3.6.
    ollama_think: bool | None = Field(default=None, alias="OLLAMA_THINK")
    triage_min_score: float = Field(default=0.5, alias="TRIAGE_MIN_SCORE")
    triage_lookback_hours: int = Field(default=24, alias="TRIAGE_LOOKBACK_HOURS")

    # Semantic layer (Option 3) — embeddings via the shared Ollama server
    # (pull the model first: `ollama pull nomic-embed-text`). Powers `digest
    # related`, semantic dedup, and `digest ask`.
    embedding_model: str = Field(default="nomic-embed-text", alias="EMBEDDING_MODEL")

    # Obsidian — vault is shared with macro digest; we land in a sibling folder
    obsidian_vault_path: str = Field(default="", alias="OBSIDIAN_VAULT_PATH")
    obsidian_digest_dir: str = Field(default="81 P&C Digest", alias="OBSIDIAN_DIGEST_DIR")
    # Capture inbox: where forwarded Telegram clips / Web Clipper .md files land.
    # Resolved against OBSIDIAN_VAULT_PATH when relative; the `clipped` ingestor
    # walks it each run and auto-keeps anything found.
    obsidian_clip_dir: str = Field(
        default="82 P&C Clipped", alias="OBSIDIAN_CLIP_DIR"
    )
    # Phase B EKG header — when true, render_daily_note prepends the "Market EKG"
    # vital-signs panel (Viz Lab winners) atop each daily note. Default off so the
    # daily note is unchanged until validated; flip to true in .env to enable.
    ekg_header_enabled: bool = Field(default=False, alias="EKG_HEADER_ENABLED")

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

    # Tiingo — free EOD price API (https://www.tiingo.com, free tier ~50 symbols/hr).
    # The alpha engine's PREFERRED price source when set: Yahoo's unauthenticated
    # chart endpoint now 429s rapid requests even from residential IPs, and Stooq
    # serves an anti-bot challenge. With a token, the price store fetches reliably;
    # empty → fall back to the (best-effort) Yahoo crumb session + Stooq.
    tiingo_api_token: str = Field(default="", alias="TIINGO_API_TOKEN")

    # LegiScan — state legislation API (free tier 30k queries/month). Feeds the
    # Lead 9 Regulatory Burden Barometer (per-state insurance-bill velocity).
    # Empty → the legiscan ingestor no-ops.
    legiscan_api_key: str = Field(default="", alias="LEGISCAN_API_KEY")

    # NAIC InsData — statutory annual-statement (Schedule P) data for the big
    # mutuals (State Farm, USAA, Liberty Mutual, …) that file nothing with the
    # SEC. InsData is a download product, so the loader reads exported files from
    # this directory (`digest ingest-naic`). Map columns in config/naic_insdata.yaml.
    naic_insdata_dir: str = Field(default="./data/naic_insdata", alias="NAIC_INSDATA_DIR")

    # Full-text article extraction — RSS/Substack feeds often ship only a teaser.
    # When a captured/feed body is shorter than fulltext_min_chars we fetch the
    # source URL and pull the main article text (trafilatura). Optional dep; the
    # whole thing degrades to the original excerpt if unavailable. Set
    # FULLTEXT_ENABLED=false to keep raw excerpts.
    fulltext_enabled: bool = Field(default=True, alias="FULLTEXT_ENABLED")
    fulltext_min_chars: int = Field(default=600, alias="FULLTEXT_MIN_CHARS")
    fulltext_max_chars: int = Field(default=8000, alias="FULLTEXT_MAX_CHARS")
    fulltext_timeout_sec: int = Field(default=12, alias="FULLTEXT_TIMEOUT_SEC")

    # Capture: X/Twitter is login-walled, so a forwarded status URL is resolved
    # to its text via the free, no-auth fxtwitter mirror API.
    x_api_base: str = Field(default="https://api.fxtwitter.com", alias="X_API_BASE")

    # Telegram push notifications — terse mobile alerts for high-conviction
    # signals, plus the interactive ask-bot (digest ask-bot) and capture inbox.
    # No-op (sends nothing) unless BOTH token + chat id are set. Get them from
    # @BotFather (token) and getUpdates / @userinfobot (chat id).
    telegram_bot_token: str = Field(default="", alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str = Field(default="", alias="TELEGRAM_CHAT_ID")
    notify_enabled: bool = Field(default=True, alias="NOTIFY_ENABLED")
    # Minimum leaderboard score a new item must reach to earn a push. PC's score
    # is an UNBOUNDED product of ~11 multipliers (not macro's 0–1 triage_score):
    # the high-conviction tier sits at ≈1.6, so that's the default gate. Tune
    # alongside signal_tiers in _meta/Scoring Weights.md once live scores settle.
    notify_min_score: float = Field(default=1.6, alias="NOTIFY_MIN_SCORE")
    # Only push items scored within this many hours — keeps pushes to genuine
    # net-new signals instead of draining the whole historical backlog.
    notify_lookback_hours: int = Field(default=24, alias="NOTIFY_LOOKBACK_HOURS")
    # Cap pushes per pipeline run so one busy day can't spam the phone.
    notify_max_per_run: int = Field(default=5, alias="NOTIFY_MAX_PER_RUN")
    # Quiet hours (local time): pushes only fire when end <= hour < start. So
    # 8/22 = "from 8am, stop after 10pm". Suppressed pushes aren't lost — the
    # next run inside the window re-evaluates them (within the lookback).
    notify_quiet_start_hour: int = Field(default=22, alias="NOTIFY_QUIET_START_HOUR")
    notify_quiet_end_hour: int = Field(default=8, alias="NOTIFY_QUIET_END_HOUR")
    # Optional once-per-run "Brief ready" ping with an obsidian:// deep link.
    notify_brief_ping: bool = Field(default=False, alias="NOTIFY_BRIEF_PING")

    # Databricks medallion sink (Wave 3 Phase 1 scaffold). All writes no-op when
    # databricks_enabled=False; workspace provisioning is deferred to Wave 4.
    # See packages/digest-core/sql/databricks/{bronze,silver,gold}.sql for DDL.
    # Shared-catalog model: one catalog (`digest`), schemas prefixed per domain
    # (pc_bronze/pc_silver/pc_gold here; macro-ai-digest uses macro_*), so both
    # digests live in one lakehouse for cross-domain queries.
    databricks_enabled: bool = Field(default=False, alias="DATABRICKS_ENABLED")
    databricks_host: str = Field(default="", alias="DATABRICKS_HOST")          # workspace URL, no scheme
    databricks_http_path: str = Field(default="", alias="DATABRICKS_HTTP_PATH") # SQL warehouse HTTP path
    databricks_token: str = Field(default="", alias="DATABRICKS_TOKEN")        # personal access token
    databricks_catalog: str = Field(default="digest", alias="DATABRICKS_CATALOG")
    databricks_schema_prefix: str = Field(default="pc_", alias="DATABRICKS_SCHEMA_PREFIX")

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

    @field_validator("telegram_bot_token", mode="before")
    @classmethod
    def _strip_bot_prefix(cls, v: str) -> str:
        """Tolerate a token pasted with the URL's 'bot' prefix (.../bot<TOKEN>).

        Real tokens always start with the bot's numeric id, so a leading 'bot'
        is the doubled-prefix mistake that yields a 404 from the Telegram API.
        """
        v = str(v).strip()
        if re.match(r"(?i)^bot\d", v):
            v = v[3:]
        return v


settings = Settings()
