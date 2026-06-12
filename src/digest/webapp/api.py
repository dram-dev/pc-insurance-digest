"""Query layer for the web observatory — pure functions over a read-only conn.

Every function takes an open ``sqlite3.Connection`` (row_factory=Row) and
returns JSON-serializable dicts. No writes, no settings access, no HTTP —
the server module owns routing and connection lifecycle; tests call these
directly against a seeded temp DB.

Accuracy conventions (mirrored in the frontend):
- All timestamps are UTC ISO strings, passed through untouched.
- "Event time" for news is ``published_at`` with ``ingested_at`` fallback;
  rows carry ``t_src`` so the UI can flag the fallback.
- Backfilled rows (historical EDGAR) are flagged via metadata ``backfill``.
- Aggregates always return ``n`` so panels can display sample sizes.
"""
from __future__ import annotations

import json
import math
import sqlite3
from typing import Any

# Latest score snapshot per item — the leaderboard's source of truth.
_LATEST_SCORES = """
    SELECT s.* FROM signal_scores s
    JOIN (
        SELECT item_id, MAX(computed_at) AS m
        FROM signal_scores GROUP BY item_id
    ) t ON t.item_id = s.item_id AND t.m = s.computed_at
"""

# Event time: published when present, else ingested (t_src flags the fallback).
_EVENT_T = "COALESCE(i.published_at, i.ingested_at)"

_FACTOR_COLS = [
    "source_mult", "regime_mult", "topic_relevance", "recency", "llm_judgment",
    "topic_boost", "burden_boost", "insurer_boost", "inflation_boost",
    "regulatory_boost", "tplf_boost", "reserve_boost",
]


def _rows(cur: sqlite3.Cursor) -> list[dict[str, Any]]:
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _one(cur: sqlite3.Cursor) -> dict[str, Any] | None:
    rows = _rows(cur)
    return rows[0] if rows else None


def _since(days: int | None) -> str:
    """UTC cutoff ISO date for an N-day window; '' means no cutoff."""
    if not days or days <= 0:
        return ""
    return f"datetime('now', '-{int(days)} days')"


def _clean_float(v: Any) -> Any:
    """JSON can't carry NaN/Inf; map them to None rather than lying."""
    if isinstance(v, float) and not math.isfinite(v):
        return None
    return v


def _item_fields(meta_json: str | None) -> dict[str, Any]:
    meta = {}
    if meta_json:
        try:
            meta = json.loads(meta_json)
        except (ValueError, TypeError):
            meta = {}
    return {
        "ticker": meta.get("ticker"),
        "form": meta.get("form"),
        "feed": meta.get("feed"),
        "backfill": bool(meta.get("backfill")),
    }


# ── Meta ─────────────────────────────────────────────────────────────────────

def meta(conn: sqlite3.Connection) -> dict[str, Any]:
    """Global context: freshness, counts, ranges, current regime, dimensions."""
    counts = {}
    for table in ("items", "signal_scores", "prices", "loss_triangles",
                  "freq_sev_signals", "run_log", "outcome_backtest"):
        counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    item_range = conn.execute(
        "SELECT MIN(COALESCE(published_at, ingested_at)),"
        "       MAX(COALESCE(published_at, ingested_at)), MAX(ingested_at)"
        " FROM items"
    ).fetchone()

    regime = _one(conn.execute(
        "SELECT as_of, market_cycle, cat_load, market_cycle_mult,"
        "       cat_load_mult, multiplier"
        " FROM regime_signals ORDER BY as_of DESC LIMIT 1"
    ))

    topics = _rows(conn.execute(
        "SELECT topic, COUNT(*) AS n FROM items"
        " WHERE topic IS NOT NULL GROUP BY topic ORDER BY n DESC"
    ))
    sources = _rows(conn.execute(
        "SELECT source, COUNT(*) AS n FROM items GROUP BY source ORDER BY n DESC"
    ))
    tickers = [r["ticker"] for r in _rows(conn.execute(
        "SELECT DISTINCT ticker, kind FROM prices ORDER BY kind, ticker"
    ))]
    last_run = conn.execute("SELECT MAX(run_at) FROM run_log").fetchone()[0]

    return {
        "counts": counts,
        "event_time_min": item_range[0],
        "event_time_max": item_range[1],
        "last_ingested_at": item_range[2],
        "last_run_at": last_run,
        "regime": regime,
        "topics": topics,
        "sources": sources,
        "price_tickers": tickers,
    }


# ── Pulse ────────────────────────────────────────────────────────────────────

def timeline(conn: sqlite3.Connection, days: int = 90) -> dict[str, Any]:
    """Daily kept-item counts per topic (UTC days, event time)."""
    cutoff = _since(days)
    where = f"AND {_EVENT_T} >= {cutoff}" if cutoff else ""
    rows = _rows(conn.execute(f"""
        SELECT substr({_EVENT_T}, 1, 10) AS day, i.topic, COUNT(*) AS n
        FROM items i
        WHERE i.triage_decision = 'keep' AND i.topic IS NOT NULL {where}
        GROUP BY 1, 2 ORDER BY 1
    """))
    return {"days": days, "rows": rows}


def events(conn: sqlite3.Connection, days: int = 90, limit: int = 400) -> dict[str, Any]:
    """Top-scored kept items in the window, as timeline events."""
    cutoff = _since(days)
    where = f"AND {_EVENT_T} >= {cutoff}" if cutoff else ""
    rows = _rows(conn.execute(f"""
        SELECT i.id, i.title, i.url, i.source, i.topic,
               i.published_at, i.ingested_at, i.metadata_json,
               CASE WHEN i.published_at IS NULL THEN 'ingested' ELSE 'published' END AS t_src,
               s.score, s.tier, s.computed_at
        FROM ({_LATEST_SCORES}) s
        JOIN items i ON i.id = s.item_id
        WHERE i.triage_decision = 'keep' {where}
        ORDER BY s.score DESC LIMIT ?
    """, (limit,)))
    for r in rows:
        r.update(_item_fields(r.pop("metadata_json")))
        r["score"] = _clean_float(r["score"])
    return {"days": days, "n": len(rows), "rows": rows}


def cadence(conn: sqlite3.Connection, days: int = 90) -> dict[str, Any]:
    """Ingestion punch card: UTC weekday × hour counts."""
    cutoff = _since(days)
    where = f"WHERE i.ingested_at >= {cutoff}" if cutoff else ""
    rows = _rows(conn.execute(f"""
        SELECT CAST(strftime('%w', i.ingested_at) AS INTEGER) AS weekday,
               CAST(strftime('%H', i.ingested_at) AS INTEGER) AS hour,
               COUNT(*) AS n
        FROM items i {where}
        GROUP BY 1, 2
    """))
    return {"days": days, "rows": rows}


def latency(conn: sqlite3.Connection, days: int = 90) -> dict[str, Any]:
    """Pickup latency per source: hours from published_at to ingested_at.

    Only rows with a real published_at qualify; negative lags (publisher clock
    ahead of ingest) are floored at 0. Quartiles computed in Python — SQLite
    has no percentile function.
    """
    cutoff = _since(days)
    where = f"AND i.ingested_at >= {cutoff}" if cutoff else ""
    rows = _rows(conn.execute(f"""
        SELECT i.source,
               MAX(0.0, (julianday(i.ingested_at) - julianday(i.published_at)) * 24.0)
                   AS lag_hours
        FROM items i
        WHERE i.published_at IS NOT NULL
          AND COALESCE(i.metadata_json, '') NOT LIKE '%"backfill": true%' {where}
    """))
    by_source: dict[str, list[float]] = {}
    for r in rows:
        if r["lag_hours"] is not None:
            by_source.setdefault(r["source"], []).append(r["lag_hours"])
    out = []
    for source, lags in by_source.items():
        lags.sort()
        n = len(lags)
        if n < 5:
            continue

        def q(p: float) -> float:
            return lags[min(n - 1, int(p * n))]

        out.append({"source": source, "n": n, "p25": q(0.25), "p50": q(0.50),
                    "p75": q(0.75), "p90": q(0.90)})
    out.sort(key=lambda r: r["p50"])
    return {"days": days, "rows": out}


def regimes(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = _rows(conn.execute(
        "SELECT as_of, market_cycle, cat_load, market_cycle_mult,"
        "       cat_load_mult, multiplier, source"
        " FROM regime_signals ORDER BY as_of"
    ))
    return {"n": len(rows), "rows": rows}


# ── Signals ──────────────────────────────────────────────────────────────────

def leaderboard(conn: sqlite3.Connection, days: int = 90, limit: int = 50) -> dict[str, Any]:
    """Latest-score leaderboard with the full multiplicative factor set."""
    cutoff = _since(days)
    where = f"AND {_EVENT_T} >= {cutoff}" if cutoff else ""
    factor_select = ", ".join(f"s.{c}" for c in _FACTOR_COLS)
    rows = _rows(conn.execute(f"""
        SELECT i.id, i.title, i.url, i.source, i.topic, i.published_at,
               i.ingested_at, i.summary, i.why_it_matters, i.metadata_json,
               s.score, s.tier, s.computed_at, s.learned_score, {factor_select}
        FROM ({_LATEST_SCORES}) s
        JOIN items i ON i.id = s.item_id
        WHERE i.triage_decision = 'keep' {where}
        ORDER BY s.score DESC LIMIT ?
    """, (limit,)))
    for r in rows:
        r.update(_item_fields(r.pop("metadata_json")))
        for c in ("score", "learned_score", *_FACTOR_COLS):
            r[c] = _clean_float(r[c])
    return {"days": days, "n": len(rows), "rows": rows}


def score_distribution(conn: sqlite3.Connection, days: int = 90) -> dict[str, Any]:
    """All latest scores in the window + observed tier boundaries."""
    cutoff = _since(days)
    where = f"AND {_EVENT_T} >= {cutoff}" if cutoff else ""
    rows = _rows(conn.execute(f"""
        SELECT s.score, s.tier
        FROM ({_LATEST_SCORES}) s
        JOIN items i ON i.id = s.item_id
        WHERE i.triage_decision = 'keep' {where}
    """))
    scores = sorted(_clean_float(r["score"]) for r in rows if r["score"] is not None)
    # Observed tier boundaries: the lowest score actually persisted per tier.
    cuts = _rows(conn.execute(f"""
        SELECT s.tier, MIN(s.score) AS cut, COUNT(*) AS n
        FROM ({_LATEST_SCORES}) s
        JOIN items i ON i.id = s.item_id
        WHERE s.tier IN ('high', 'medium') {where}
        GROUP BY s.tier
    """))
    tiers: dict[str, int] = {}
    for r in rows:
        tiers[r["tier"] or "untiered"] = tiers.get(r["tier"] or "untiered", 0) + 1
    return {"days": days, "n": len(scores), "scores": scores,
            "tier_counts": tiers,
            "tier_cuts": {r["tier"]: _clean_float(r["cut"]) for r in cuts}}


# ── Market ───────────────────────────────────────────────────────────────────

def prices(conn: sqlite3.Connection, days: int = 365,
           tickers: list[str] | None = None) -> dict[str, Any]:
    cutoff = _since(days)
    where = f"AND date >= date({cutoff})" if cutoff else ""
    params: list[Any] = []
    tick_where = ""
    if tickers:
        tick_where = f"AND ticker IN ({','.join('?' * len(tickers))})"
        params = list(tickers)
    rows = _rows(conn.execute(f"""
        SELECT ticker, date, close, kind FROM prices
        WHERE 1=1 {where} {tick_where}
        ORDER BY ticker, date
    """, params))
    series: dict[str, dict[str, Any]] = {}
    for r in rows:
        s = series.setdefault(r["ticker"], {"ticker": r["ticker"], "kind": r["kind"],
                                            "dates": [], "closes": []})
        s["dates"].append(r["date"])
        s["closes"].append(_clean_float(r["close"]))
    return {"days": days, "series": list(series.values())}


def price_events(conn: sqlite3.Connection, days: int = 365) -> dict[str, Any]:
    """EDGAR filing events for marker overlay on the price chart."""
    cutoff = _since(days)
    where = f"AND {_EVENT_T} >= {cutoff}" if cutoff else ""
    rows = _rows(conn.execute(f"""
        SELECT i.id, i.title, i.url, i.published_at, i.ingested_at,
               json_extract(i.metadata_json, '$.ticker') AS ticker,
               json_extract(i.metadata_json, '$.form') AS form,
               COALESCE(json_extract(i.metadata_json, '$.backfill'), 0) AS backfill
        FROM items i
        WHERE i.source = 'edgar'
          AND json_extract(i.metadata_json, '$.ticker') IS NOT NULL {where}
        ORDER BY {_EVENT_T}
    """))
    return {"days": days, "n": len(rows), "rows": rows}


def forecasts(conn: sqlite3.Connection) -> dict[str, Any]:
    """Latest return forecasts per (ticker, horizon) + their model scorecards."""
    rows = _rows(conn.execute("""
        SELECT f.ticker, f.as_of, f.horizon_days, f.pred_excess, f.pred_prob,
               f.model_id, m.algo, m.ic, m.baseline_ic, m.hit_rate,
               m.n_samples, m.long_short_ret, m.trained_at
        FROM return_forecasts f
        JOIN (
            SELECT ticker, horizon_days, MAX(as_of) AS m
            FROM return_forecasts GROUP BY ticker, horizon_days
        ) latest ON latest.ticker = f.ticker
              AND latest.horizon_days = f.horizon_days AND latest.m = f.as_of
        LEFT JOIN return_models m ON m.id = f.model_id
        ORDER BY f.horizon_days, f.pred_excess DESC
    """))
    for r in rows:
        for c in ("pred_excess", "pred_prob", "ic", "baseline_ic",
                  "hit_rate", "long_short_ret"):
            r[c] = _clean_float(r[c])
    return {"n": len(rows), "rows": rows}


# ── Loss Lab ─────────────────────────────────────────────────────────────────

def triangle_catalog(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = _rows(conn.execute("""
        SELECT insurer, lob, canonical_lob, metric,
               MAX(as_of) AS latest_as_of, COUNT(*) AS cells,
               COUNT(DISTINCT accident_year) AS years,
               MAX(cumulative_value) AS peak_value
        FROM loss_triangles
        WHERE insurer != 'TEST'
        GROUP BY insurer, lob, metric
        ORDER BY insurer, peak_value DESC
    """))
    return {"n": len(rows), "rows": rows}


def triangle(conn: sqlite3.Connection, insurer: str, lob: str,
             metric: str = "incurred") -> dict[str, Any]:
    as_of = conn.execute(
        "SELECT MAX(as_of) FROM loss_triangles"
        " WHERE insurer=? AND lob=? AND metric=?",
        (insurer, lob, metric),
    ).fetchone()[0]
    if as_of is None:
        return {"insurer": insurer, "lob": lob, "metric": metric,
                "as_of": None, "cells": []}
    cells = _rows(conn.execute("""
        SELECT accident_year, dev_period, cumulative_value
        FROM loss_triangles
        WHERE insurer=? AND lob=? AND metric=? AND as_of=?
        ORDER BY accident_year, dev_period
    """, (insurer, lob, metric, as_of)))
    for c in cells:
        c["cumulative_value"] = _clean_float(c["cumulative_value"])
    return {"insurer": insurer, "lob": lob, "metric": metric,
            "as_of": as_of, "cells": cells}


def freq_sev(conn: sqlite3.Connection, insurer: str) -> dict[str, Any]:
    rows = _rows(conn.execute("""
        SELECT f.insurer, f.grain, f.lob, f.accident_year, f.reported_claims,
               f.incurred_musd, f.earned_premium_musd, f.severity_usd,
               f.frequency_per_musd, f.pure_premium_ratio, f.as_of
        FROM freq_sev_signals f
        JOIN (
            SELECT insurer, grain, lob, accident_year, MAX(as_of) AS m
            FROM freq_sev_signals GROUP BY insurer, grain, lob, accident_year
        ) t ON t.insurer = f.insurer AND t.grain = f.grain AND t.lob = f.lob
           AND t.accident_year = f.accident_year AND t.m = f.as_of
        WHERE f.insurer = ?
        ORDER BY f.lob, f.accident_year
    """, (insurer,)))
    for r in rows:
        for c in ("reported_claims", "incurred_musd", "earned_premium_musd",
                  "severity_usd", "frequency_per_musd", "pure_premium_ratio"):
            r[c] = _clean_float(r[c])
    return {"insurer": insurer, "n": len(rows), "rows": rows}


def freq_sev_insurers(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = _rows(conn.execute(
        "SELECT insurer, COUNT(*) AS n FROM freq_sev_signals"
        " GROUP BY insurer ORDER BY n DESC"
    ))
    return {"rows": rows}


def reserving(conn: sqlite3.Connection) -> dict[str, Any]:
    """Latest reserving signal per (insurer, lob, metric)."""
    rows = _rows(conn.execute("""
        SELECT r.insurer, r.lob, r.metric, r.as_of, r.ultimate, r.latest,
               r.ibnr, r.prior_ibnr, r.deterioration_pct, r.direction
        FROM reserving_signals r
        JOIN (
            SELECT insurer, lob, metric, MAX(as_of) AS m
            FROM reserving_signals GROUP BY insurer, lob, metric
        ) t ON t.insurer = r.insurer AND t.lob = r.lob
           AND t.metric = r.metric AND t.m = r.as_of
        ORDER BY r.deterioration_pct DESC
    """))
    for r in rows:
        for c in ("ultimate", "latest", "ibnr", "prior_ibnr", "deterioration_pct"):
            r[c] = _clean_float(r[c])
    return {"n": len(rows), "rows": rows}


def severity(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = _rows(conn.execute("""
        SELECT index_name, observation_date, value, zscore_12m,
               is_anomaly, category, source
        FROM severity_index ORDER BY index_name, observation_date
    """))
    for r in rows:
        r["value"] = _clean_float(r["value"])
        r["zscore_12m"] = _clean_float(r["zscore_12m"])
    return {"n": len(rows), "rows": rows}


# ── Operations ───────────────────────────────────────────────────────────────

def ops_runs(conn: sqlite3.Connection, days: int = 30) -> dict[str, Any]:
    """Ingest activity matrix: source × UTC day, plus recent errors."""
    cutoff = _since(days)
    where = f"WHERE run_at >= {cutoff}" if cutoff else ""
    rows = _rows(conn.execute(f"""
        SELECT source, substr(run_at, 1, 10) AS day,
               SUM(COALESCE(items_new, 0)) AS items_new,
               SUM(COALESCE(items_fetched, 0)) AS items_fetched,
               COUNT(*) AS runs,
               SUM(CASE WHEN status != 'ok' THEN 1 ELSE 0 END) AS failures
        FROM run_log {where}
        GROUP BY source, day ORDER BY day
    """))
    errors = _rows(conn.execute(f"""
        SELECT run_at, run_type, source, status, error
        FROM run_log
        {where + (' AND' if where else 'WHERE')} status != 'ok'
        ORDER BY run_at DESC LIMIT 25
    """))
    return {"days": days, "rows": rows, "errors": errors}


def ops_funnel(conn: sqlite3.Connection, days: int = 30) -> dict[str, Any]:
    """Cohort funnel by UTC ingest day: new → kept → summarized."""
    cutoff = _since(days)
    where = f"WHERE ingested_at >= {cutoff}" if cutoff else ""
    rows = _rows(conn.execute(f"""
        SELECT substr(ingested_at, 1, 10) AS day,
               COUNT(*) AS ingested,
               SUM(CASE WHEN triage_decision = 'keep' THEN 1 ELSE 0 END) AS kept,
               SUM(CASE WHEN summarized_at IS NOT NULL THEN 1 ELSE 0 END) AS summarized
        FROM items {where}
        GROUP BY day ORDER BY day
    """))
    return {"days": days, "rows": rows}


def ops_summarizer(conn: sqlite3.Connection, days: int = 30) -> dict[str, Any]:
    """Per-day summarizer latency: median + p90 computed in Python."""
    cutoff = _since(days)
    where = f"AND run_at >= {cutoff}" if cutoff else ""
    rows = _rows(conn.execute(f"""
        SELECT substr(run_at, 1, 10) AS day, backend, duration_ms
        FROM summarizer_log
        WHERE status = 'ok' AND duration_ms IS NOT NULL {where}
        ORDER BY run_at
    """))
    by_day: dict[str, list[int]] = {}
    backends: set[str] = set()
    for r in rows:
        by_day.setdefault(r["day"], []).append(r["duration_ms"])
        backends.add(r["backend"])
    out = []
    for day, durs in sorted(by_day.items()):
        durs.sort()
        n = len(durs)
        out.append({
            "day": day, "n": n,
            "p50": durs[n // 2],
            "p90": durs[min(n - 1, int(0.9 * n))],
        })
    return {"days": days, "backends": sorted(backends), "rows": out}


def outcomes(conn: sqlite3.Connection) -> dict[str, Any]:
    """Outcome-label corroboration rates, overall and by source/topic."""
    by_horizon = _rows(conn.execute("""
        SELECT horizon_days, COUNT(*) AS n, SUM(corroborated) AS corroborated
        FROM outcome_backtest GROUP BY horizon_days ORDER BY horizon_days
    """))
    by_source = _rows(conn.execute("""
        SELECT i.source, o.horizon_days, COUNT(*) AS n,
               SUM(o.corroborated) AS corroborated
        FROM outcome_backtest o JOIN items i ON i.id = o.item_id
        GROUP BY i.source, o.horizon_days HAVING n >= 10
        ORDER BY o.horizon_days, n DESC
    """))
    by_topic = _rows(conn.execute("""
        SELECT i.topic, o.horizon_days, COUNT(*) AS n,
               SUM(o.corroborated) AS corroborated
        FROM outcome_backtest o JOIN items i ON i.id = o.item_id
        WHERE i.topic IS NOT NULL
        GROUP BY i.topic, o.horizon_days HAVING n >= 10
        ORDER BY o.horizon_days, n DESC
    """))
    return {"by_horizon": by_horizon, "by_source": by_source, "by_topic": by_topic}
