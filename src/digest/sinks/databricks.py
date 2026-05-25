"""Databricks medallion sink — live writes at each pipeline checkpoint.

Architecture (see ~/.claude/plans/woolly-hatching-gem.md and
packages/digest-core/sql/databricks/{bronze,silver,gold}.sql):

  bronze.ingested_items   ← write_ingested()    every IngestedItem, incl. drops
  bronze.fred_observations ← write_fred_observations()   full monthly series
  bronze.regime_signals   ← write_regime()      regime detector outputs
  bronze.pipeline_telemetry ← write_telemetry() per-stage timing/errors
  silver.triage_verdicts  ← write_triage()      verdict + topic + burden
  silver.signal_scores    ← write_score()       all 10 boost factors
  silver.summaries        ← write_summary()     materiality + summary text

Join key: item_hash = sha256(source || '::' || source_id), derived here at
write time. SQLite stays untouched.

Operational discipline:
- No-op when settings.databricks_enabled is False (default).
- databricks-sql-connector is imported lazily inside _connection() so users
  without DATABRICKS_ENABLED don't need the dependency installed.
- All writes are best-effort: warnings logged, exceptions swallowed. A
  transient Databricks outage never bricks the local pipeline (SQLite is
  the source of truth; medallion can always be backfilled from it).
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, is_dataclass
from datetime import datetime
from typing import Any, Iterable

from digest.config import Settings

logger = logging.getLogger(__name__)


def item_hash(source: str, source_id: str) -> str:
    """Stable medallion join key derived from the SQLite natural key."""
    raw = f"{source}::{source_id}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _iso(ts: datetime | str | None) -> str | None:
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return ts.isoformat(timespec="seconds")
    return str(ts)[:19]


def _to_dict(obj: Any) -> dict[str, Any]:
    """Coerce an IngestedItem dataclass (or already-dict) into a dict."""
    if is_dataclass(obj):
        return asdict(obj)
    if isinstance(obj, dict):
        return obj
    raise TypeError(f"Expected dict or dataclass, got {type(obj).__name__}")


class DatabricksSink:
    """Best-effort writer into the medallion tables. No-op when disabled."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._enabled = settings.databricks_enabled
        self._conn: Any | None = None  # lazy databricks.sql.Connection

    # ── Connection (lazy) ─────────────────────────────────────────────────

    def _connection(self) -> Any:
        if self._conn is not None:
            return self._conn
        try:
            from databricks import sql  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError(
                "DATABRICKS_ENABLED=true but databricks-sql-connector is not "
                "installed. Run `uv add databricks-sql-connector`."
            ) from exc
        self._conn = sql.connect(
            server_hostname=self._settings.databricks_host,
            http_path=self._settings.databricks_http_path,
            access_token=self._settings.databricks_token,
        )
        cur = self._conn.cursor()
        try:
            cur.execute(f"USE CATALOG `{self._settings.databricks_catalog}`")
        finally:
            cur.close()
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001
                pass
            self._conn = None

    # ── Bronze writers ────────────────────────────────────────────────────

    def write_ingested(self, items: Iterable[Any]) -> None:
        """One row per IngestedItem (dataclass or dict). Drops included."""
        if not self._enabled:
            return
        rows: list[dict[str, Any]] = []
        for raw in items:
            it = _to_dict(raw)
            src = it.get("source") or ""
            sid = it.get("source_id") or ""
            if not src or not sid:
                continue
            rows.append({
                "item_hash":     item_hash(src, sid),
                "source":        src,
                "source_id":     sid,
                "url":           it.get("url"),
                "title":         it.get("title") or "",
                "author":        it.get("author"),
                "content":       it.get("content"),
                "published_at":  _iso(it.get("published_at")),
                "ingested_at":   _iso(it.get("ingested_at")) or _iso(datetime.utcnow()),
                "metadata_json": json.dumps(it.get("metadata") or {}, default=str),
                "topic_hint":    (it.get("metadata") or {}).get("topic_hint"),
            })
        self._insert("bronze.ingested_items", rows)

    def write_fred_observations(self, rows: Iterable[dict[str, Any]]) -> None:
        """Full monthly FRED observations — store the whole series, not just anomalies."""
        if not self._enabled:
            return
        self._insert("bronze.fred_observations", [dict(r) for r in rows])

    def write_regime(self, regime: dict[str, Any]) -> None:
        if not self._enabled:
            return
        self._insert("bronze.regime_signals", [dict(regime)])

    def write_telemetry(self, telemetry: dict[str, Any]) -> None:
        """One row per pipeline stage execution. Subsumes run_log + summarizer_log."""
        if not self._enabled:
            return
        self._insert("bronze.pipeline_telemetry", [dict(telemetry)])

    # ── Silver writers ────────────────────────────────────────────────────

    def write_triage(self, source: str, source_id: str, verdict: dict[str, Any]) -> None:
        if not self._enabled:
            return
        row = {
            "item_hash":        item_hash(source, source_id),
            "triaged_at":       _iso(verdict.get("triaged_at")) or _iso(datetime.utcnow()),
            "decision":         verdict.get("decision"),
            "score":            verdict.get("score"),
            "topic":            verdict.get("topic"),
            "sub_tags":         verdict.get("sub_tags") or [],
            "confidence":       verdict.get("confidence"),
            "reason":           verdict.get("reason"),
            "burden_direction": verdict.get("burden_direction"),
            "burden_intensity": verdict.get("burden_intensity"),
            "model_id":         verdict.get("model_id"),
        }
        self._insert("silver.triage_verdicts", [row])

    def write_score(self, source: str, source_id: str, score_row: dict[str, Any]) -> None:
        """All 10 multiplicative factors broken out — enables back-test analysis."""
        if not self._enabled:
            return
        row = {
            "item_hash":        item_hash(source, source_id),
            "computed_at":      _iso(score_row.get("computed_at")) or _iso(datetime.utcnow()),
            "score":            score_row.get("score"),
            "source_mult":      score_row.get("source_mult"),
            "regime_mult":      score_row.get("regime_mult"),
            "topic_relevance": score_row.get("topic_relevance"),
            "recency":          score_row.get("recency"),
            "llm_judgment":     score_row.get("llm_judgment"),
            "topic_boost":      score_row.get("topic_boost"),
            "burden_boost":     score_row.get("burden_boost"),
            "insurer_boost":    score_row.get("insurer_boost"),
            "inflation_boost":  score_row.get("inflation_boost"),
            "regulatory_boost": score_row.get("regulatory_boost"),
            "tplf_boost":       score_row.get("tplf_boost"),
        }
        self._insert("silver.signal_scores", [row])

    def write_summary(self, source: str, source_id: str, summary: dict[str, Any]) -> None:
        if not self._enabled:
            return
        row = {
            "item_hash":      item_hash(source, source_id),
            "summarized_at":  _iso(summary.get("summarized_at")) or _iso(datetime.utcnow()),
            "summary":        summary.get("summary"),
            "why_it_matters": summary.get("why_it_matters"),
            "see_also":       summary.get("see_also"),
            "materiality":    summary.get("materiality"),
            "confidence":     summary.get("confidence"),
            "input_chars":    summary.get("input_chars"),
            "output_chars":   summary.get("output_chars"),
            "model_id":       summary.get("model_id"),
            "duration_ms":    summary.get("duration_ms"),
        }
        self._insert("silver.summaries", [row])

    # ── Internals ─────────────────────────────────────────────────────────

    def _insert(self, table: str, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        try:
            conn = self._connection()
            cur = conn.cursor()
            try:
                cols = list(rows[0].keys())
                col_str = ", ".join(f"`{c}`" for c in cols)
                placeholders = ", ".join("?" for _ in cols)
                stmt = f"INSERT INTO {table} ({col_str}) VALUES ({placeholders})"
                values = [tuple(r.get(c) for c in cols) for r in rows]
                cur.executemany(stmt, values)
            finally:
                cur.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "databricks sink %s INSERT failed (%d rows): %s — swallowed",
                table, len(rows), exc,
            )
