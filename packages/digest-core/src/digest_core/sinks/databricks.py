"""Databricks medallion sink — live writes at each pipeline checkpoint.

Domain-agnostic; configured via constructor args from each project's settings.

Architecture (originated in pc-insurance-digest; lifted to core 2026-05-25):

  bronze.ingested_items   ← write_ingested()       every IngestedItem, incl. drops
  bronze.item_embeddings  ← write_embedding()      semantic-layer vectors
  bronze.fred_observations ← write_fred_observations() full monthly series
  bronze.regime_signals   ← write_regime()          regime detector outputs
  bronze.pipeline_telemetry ← write_telemetry()     per-stage timing/errors
  silver.triage_verdicts  ← write_triage()          verdict + topic + burden
  silver.signal_scores    ← write_score()           all boost factors
  silver.summaries        ← write_summary()         materiality + summary text
  silver.manual_ratings   ← write_rating()          user calibration ratings
  silver.outcome_backtest ← write_outcome()         did a ranked item corroborate?
  silver.learned_scores   ← write_learned_score()   learned relevance (Option 4)
  silver.reserving_signals ← write_reserving()       chain-ladder IBNR (Option 5)

Wave 4 — Insurance EKG leads (no-op scaffolds until each lead's ingestor ships):
  bronze.reinsurance_pricing ← write_reinsurance_pricing()  Lead 1 ROL/ILS series
  bronze.cat_load_nowcast    ← write_cat_load_nowcast()      Lead 2 hazard nowcast
  bronze.severity_index      ← write_severity_index()        Lead 3 severity tape
  silver.litigation_pressure ← write_litigation_pressure()   Lead 4 verdict/TPLF index
  silver.disclosure_sentiment ← write_disclosure_sentiment() Lead 5 reserve-tone NLP
  silver.capital_flows       ← write_capital_flow()          Lead 8 funding/M&A facts

Join key: `item_hash = sha256(source || '::' || source_id)`, derived here at
write time. SQLite stays untouched.

Operational discipline:
- No-op when `enabled` is False (constructor arg).
- databricks-sql-connector is imported lazily inside `_connection()` so
  consumers without DATABRICKS_ENABLED don't need the dependency installed.
- All writes are best-effort: warnings logged, exceptions swallowed. A
  transient Databricks outage never bricks the local pipeline (SQLite is
  the source of truth; medallion can always be backfilled from it).
- _insert() uses batched MERGE INTO with UNION ALL subquery (Databricks SQL
  rejects `USING (VALUES ...) AS s(col1, ...)` column-alias form in MERGE,
  COLUMN_ALIASES_NOT_ALLOWED). ~50× fewer round-trips than per-row INSERT.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, is_dataclass
from datetime import datetime
from typing import Any, Iterable

logger = logging.getLogger(__name__)


# Primary-key columns per medallion table. Drives _insert()'s MERGE INTO ... ON
# construction so re-ingests don't double-write — Delta doesn't enforce PK
# constraints, the DDL hint is informational only.
_PRIMARY_KEYS: dict[str, tuple[str, ...]] = {
    "bronze.ingested_items":     ("item_hash",),
    "bronze.item_embeddings":    ("item_hash",),
    "bronze.fred_observations":  ("series_id", "observation_date"),
    "bronze.regime_signals":     ("as_of", "source"),
    "bronze.pipeline_telemetry": ("run_id", "stage", "source"),
    "silver.triage_verdicts":    ("item_hash", "triaged_at"),
    "silver.signal_scores":      ("item_hash", "computed_at"),
    "silver.summaries":          ("item_hash", "summarized_at"),
    "silver.manual_ratings":     ("item_hash", "rated_at"),
    "silver.outcome_backtest":   ("item_hash", "horizon_days"),
    "silver.learned_scores":     ("item_hash", "model_id"),
    "silver.reserving_signals":  ("insurer", "lob", "metric", "as_of"),
    # Wave 4 — Insurance EKG leads.
    "bronze.reinsurance_pricing":  ("index_name", "observation_date"),
    "bronze.cat_load_nowcast":     ("metric_name", "region", "observation_date"),
    "bronze.severity_index":       ("index_name", "observation_date"),
    "silver.litigation_pressure":  ("state", "sector", "as_of"),
    "silver.disclosure_sentiment": ("insurer", "period", "as_of"),
    "silver.capital_flows":        ("item_hash",),
}

# Max rows per MERGE statement. 50 keeps total parameter count well under
# warehouse limits (50 × ~15 cols ≈ 750 params) and bounds the size of any
# single failed round-trip.
_BATCH_SIZE = 50


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
    """Best-effort batched writer into the medallion tables. No-op when disabled."""

    def __init__(
        self,
        enabled: bool,
        host: str,
        http_path: str,
        token: str,
        catalog: str,
        schema_prefix: str = "",
    ) -> None:
        self._enabled = enabled
        self._host = host
        self._http_path = http_path
        self._token = token
        self._catalog = catalog
        # Prepended to the medallion schema so multiple domains can share one
        # catalog without colliding: "" → bronze.*; "pc_" → pc_bronze.*;
        # "macro_" → macro_bronze.*. Table names + primary-key lookups stay
        # unprefixed internally; only the emitted SQL is qualified.
        self._schema_prefix = schema_prefix
        self._conn: Any | None = None  # lazy databricks.sql.Connection

    def _qualify(self, table: str) -> str:
        """`bronze.ingested_items` → `{prefix}bronze.ingested_items`."""
        schema, _, tbl = table.partition(".")
        return f"{self._schema_prefix}{schema}.{tbl}"

    # ── Connection (lazy) ─────────────────────────────────────────────────

    def _connection(self) -> Any:
        if self._conn is not None:
            return self._conn
        try:
            from databricks import sql  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError(
                "DATABRICKS_ENABLED=true but databricks-sql-connector is not "
                "installed. Add `databricks-sql-connector` to the host project."
            ) from exc
        self._conn = sql.connect(
            server_hostname=self._host,
            http_path=self._http_path,
            access_token=self._token,
        )
        cur = self._conn.cursor()
        try:
            cur.execute(f"USE CATALOG `{self._catalog}`")
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
        if not self._enabled:
            return
        self._insert("bronze.fred_observations", [dict(r) for r in rows])

    def write_regime(self, regime: dict[str, Any]) -> None:
        if not self._enabled:
            return
        self._insert("bronze.regime_signals", [dict(regime)])

    def write_telemetry(self, telemetry: dict[str, Any]) -> None:
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
        """Domain pipelines pass through their boost-factor breakdown unchanged
        — each domain's silver.signal_scores DDL declares its own column set."""
        if not self._enabled:
            return
        row = {
            "item_hash":        item_hash(source, source_id),
            "computed_at":      _iso(score_row.get("computed_at")) or _iso(datetime.utcnow()),
            "score":            score_row.get("score"),
            "source_mult":      score_row.get("source_mult"),
            "regime_mult":      score_row.get("regime_mult"),
            "topic_relevance":  score_row.get("topic_relevance"),
            "recency":          score_row.get("recency"),
            "llm_judgment":     score_row.get("llm_judgment"),
            "topic_boost":      score_row.get("topic_boost"),
            "burden_boost":     score_row.get("burden_boost"),
            "insurer_boost":    score_row.get("insurer_boost"),
            "inflation_boost":  score_row.get("inflation_boost"),
            "regulatory_boost": score_row.get("regulatory_boost"),
            "tplf_boost":       score_row.get("tplf_boost"),
            "tier":             score_row.get("tier"),
            "reserve_boost":    score_row.get("reserve_boost"),
            "learned_score":    score_row.get("learned_score"),
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

    def write_embedding(self, source: str, source_id: str, emb: dict[str, Any]) -> None:
        """One embedding vector per item → bronze.item_embeddings (semantic layer).
        vector_json is the JSON-encoded float list; promote to a real ARRAY/Vector
        type when moving to native Databricks Vector Search."""
        if not self._enabled:
            return
        row = {
            "item_hash":   item_hash(source, source_id),
            "model":       emb.get("model"),
            "dim":         emb.get("dim"),
            "vector_json": emb.get("vector_json"),
            "computed_at": _iso(emb.get("computed_at")) or _iso(datetime.utcnow()),
        }
        self._insert("bronze.item_embeddings", [row])

    def write_rating(self, source: str, source_id: str, rating: dict[str, Any]) -> None:
        """User's manual rating of an item — the calibration input that powers
        gold.score_calibration (system score vs. what the user thinks it's worth)."""
        if not self._enabled:
            return
        row = {
            "item_hash":   item_hash(source, source_id),
            "rated_at":    _iso(rating.get("rated_at")) or _iso(datetime.utcnow()),
            "user_rating": rating.get("user_rating"),
            "note":        rating.get("note"),
        }
        self._insert("silver.manual_ratings", [row])

    def write_reserving(self, sig: dict[str, Any]) -> None:
        """Chain-ladder reserving estimate (Option 5) → silver.reserving_signals.
        Insurer/LOB-keyed (not item_hash) — a derived actuarial fact, not a news item."""
        if not self._enabled:
            return
        row = {
            "insurer":           sig.get("insurer"),
            "lob":               sig.get("lob"),
            "metric":            sig.get("metric"),
            "as_of":             _iso(sig.get("as_of")) or _iso(datetime.utcnow()),
            "ultimate":          sig.get("ultimate"),
            "latest":            sig.get("latest"),
            "ibnr":              sig.get("ibnr"),
            "prior_ibnr":        sig.get("prior_ibnr"),
            "deterioration_pct": sig.get("deterioration_pct"),
            "direction":         sig.get("direction"),
        }
        self._insert("silver.reserving_signals", [row])

    def write_learned_score(self, source: str, source_id: str, ls: dict[str, Any]) -> None:
        """Per-item learned relevance score (Option 4) → silver.learned_scores,
        for A/B against the heuristic score in gold."""
        if not self._enabled:
            return
        row = {
            "item_hash":     item_hash(source, source_id),
            "model_id":      ls.get("model_id"),
            "learned_score": ls.get("learned_score"),
            "scored_at":     _iso(ls.get("scored_at")) or _iso(datetime.utcnow()),
        }
        self._insert("silver.learned_scores", [row])

    def write_outcome(self, source: str, source_id: str, outcome: dict[str, Any]) -> None:
        """Backtest outcome for an item at one horizon — did it corroborate?
        Feeds gold.outcome_hit_rate + the learned scorer's labels."""
        if not self._enabled:
            return
        signals = outcome.get("signals") or []
        row = {
            "item_hash":       item_hash(source, source_id),
            "horizon_days":    outcome.get("horizon_days"),
            "checked_at":      _iso(outcome.get("checked_at")) or _iso(datetime.utcnow()),
            "corroborated":    bool(outcome.get("corroborated")),
            "signals":         signals if isinstance(signals, list) else [signals],
            "followon_count":  outcome.get("followon_count"),
            "edgar_filed":     bool(outcome.get("edgar_filed")),
            "regime_shifted":  bool(outcome.get("regime_shifted")),
            "manual_rating":   outcome.get("manual_rating"),
            "stock_move_z":    outcome.get("stock_move_z"),
            "stock_move_band": outcome.get("stock_move_band"),
        }
        self._insert("silver.outcome_backtest", [row])

    # ── Wave 4 EKG writers (no-op until each lead's ingestor ships) ─────────

    def write_reinsurance_pricing(self, rows: Iterable[dict[str, Any]]) -> None:
        """Lead 1 — GuyCarp ROL / Artemis ILS series → bronze.reinsurance_pricing.
        Rows mirror the fred_observations shape (one per index per date)."""
        if not self._enabled:
            return
        self._insert("bronze.reinsurance_pricing", [dict(r) for r in rows])

    def write_cat_load_nowcast(self, rows: Iterable[dict[str, Any]]) -> None:
        """Lead 2 — OpenFEMA / NOAA CPC / drought / outage → bronze.cat_load_nowcast."""
        if not self._enabled:
            return
        self._insert("bronze.cat_load_nowcast", [dict(r) for r in rows])

    def write_severity_index(self, rows: Iterable[dict[str, Any]]) -> None:
        """Lead 3 — Manheim UVVI + FRED parts/labor/medical → bronze.severity_index."""
        if not self._enabled:
            return
        self._insert("bronze.severity_index", [dict(r) for r in rows])

    def write_litigation_pressure(self, sig: dict[str, Any]) -> None:
        """Lead 4 — per-state × sector verdict/TPLF index → silver.litigation_pressure.
        (state, sector)-keyed derived fact, not a news item."""
        if not self._enabled:
            return
        self._insert("silver.litigation_pressure", [dict(sig)])

    def write_disclosure_sentiment(self, sig: dict[str, Any]) -> None:
        """Lead 5 — reserve-tone NLP over EDGAR filings → silver.disclosure_sentiment.
        (insurer, period)-keyed."""
        if not self._enabled:
            return
        self._insert("silver.disclosure_sentiment", [dict(sig)])

    def write_capital_flow(self, source: str, source_id: str, flow: dict[str, Any]) -> None:
        """Lead 8 — extracted funding-round / M&A deal facts → silver.capital_flows.
        item_hash-keyed back to the source news item."""
        if not self._enabled:
            return
        row = {
            "item_hash":  item_hash(source, source_id),
            "as_of":      _iso(flow.get("as_of")) or _iso(datetime.utcnow()),
            "deal_type":  flow.get("deal_type"),
            "amount_usd": flow.get("amount_usd"),
            "stage":      flow.get("stage"),
            "target":     flow.get("target"),
            "investors":  flow.get("investors"),
        }
        self._insert("silver.capital_flows", [row])

    # ── Internals ─────────────────────────────────────────────────────────

    def _insert(self, table: str, rows: list[dict[str, Any]]) -> None:
        """Idempotent batched MERGE INTO write."""
        if not rows:
            return
        pk_cols = _PRIMARY_KEYS.get(table)
        if pk_cols is None:
            logger.warning(
                "databricks sink: no primary key registered for %s — skipping write",
                table,
            )
            return
        cols = list(rows[0].keys())
        qualified = self._qualify(table)
        try:
            conn = self._connection()
            cur = conn.cursor()
            try:
                for start in range(0, len(rows), _BATCH_SIZE):
                    batch = rows[start:start + _BATCH_SIZE]
                    self._merge_batch(cur, qualified, cols, pk_cols, batch)
            finally:
                cur.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "databricks sink %s MERGE failed (%d rows): %s — swallowed",
                qualified, len(rows), exc,
            )

    @staticmethod
    def _merge_batch(
        cur: Any,
        table: str,
        cols: list[str],
        pk_cols: tuple[str, ...],
        rows: list[dict[str, Any]],
    ) -> None:
        # Databricks SQL rejects `USING (VALUES ...) AS s(col1, ...)` in MERGE
        # — column aliases on a VALUES table aren't allowed. Wrap rows in a
        # UNION ALL of SELECTs instead: the first SELECT names columns via
        # `AS c`, subsequent SELECTs are positional matches.
        first_cols = ", ".join(f"? AS `{c}`" for c in cols)
        first_select = f"SELECT {first_cols}"
        more_selects = " UNION ALL ".join(
            f"SELECT {', '.join('?' for _ in cols)}"
            for _ in rows[1:]
        )
        inner = first_select + (" UNION ALL " + more_selects if more_selects else "")

        col_list    = ", ".join(f"`{c}`" for c in cols)
        on_clause   = " AND ".join(f"t.`{c}` = s.`{c}`" for c in pk_cols)
        insert_vals = ", ".join(f"s.`{c}`" for c in cols)
        stmt = (
            f"MERGE INTO {table} t "
            f"USING ({inner}) s "
            f"ON {on_clause} "
            f"WHEN NOT MATCHED THEN INSERT ({col_list}) VALUES ({insert_vals})"
        )
        params = [r.get(c) for r in rows for c in cols]
        cur.execute(stmt, params)
