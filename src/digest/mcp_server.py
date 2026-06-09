"""FastMCP server — the PC Digest data-analyst agent.

Exposes the digest's SQLite warehouse to an MCP client (Claude Code / Claude
Desktop) as a senior P&C-insurance data analyst's tool belt. The agent's *skills*
are the composition of three things this module ships:

  1. Tools     — read-only SQL plus domain-aware shortcuts (signal-score
                 root-cause decomposition, pipeline-health forensics,
                 per-source quality, leaderboard top-N, a data overview).
  2. Resources — a hand-authored data dictionary (`schema://overview`) and the
                 leaderboard scoring formula (`formula://scoring`), so the model
                 knows what every table and multiplier *means*, not just its type.
  3. A prompt  — `analyst`, which frames the persona + a hypothesis→query→verify
                 →root-cause methodology and insists every claim be grounded in
                 a query.

The client's model is the analyst; this server is its safe, grounded interface to
the data. All DB access is via a read-only SQLite connection (``mode=ro``), so no
tool — including arbitrary ``run_sql`` — can mutate the source of truth.

Run (stdio transport):

    uv run --extra mcp digest-mcp

Wired via the repo's ``.mcp.json`` so Claude Code auto-discovers it here.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from digest.config import settings

mcp = FastMCP("Agent Server")

# Row caps so a careless query can't drag the whole warehouse through the
# context window. run_sql clamps its caller-supplied limit into [1, _MAX_ROWS].
_DEFAULT_ROWS = 200
_MAX_ROWS = 2000


# ── Read-only DB access ───────────────────────────────────────────────────


def _ro_conn() -> sqlite3.Connection:
    """Open the configured SQLite DB read-only (``mode=ro``).

    Read-only is the hard guarantee that no tool can write — it holds even for
    arbitrary ``run_sql``. Raises FileNotFoundError with a runnable hint when the
    DB hasn't been initialised yet.
    """
    path = Path(settings.db_path)
    if not path.exists():
        raise FileNotFoundError(
            f"No digest DB at {path}. Run `uv run digest init-db` first "
            f"(or set DB_PATH in .env to point at an existing warehouse)."
        )
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def _json(obj: Any) -> str:
    """Stable, readable JSON for tool results (str-coerce odd types)."""
    return json.dumps(obj, indent=2, default=str, ensure_ascii=False)


def _rows(cur: sqlite3.Cursor) -> list[dict[str, Any]]:
    """Materialise a cursor as a list of plain dicts (JSON-safe)."""
    return [dict(r) for r in cur.fetchall()]


def _table_names(conn: sqlite3.Connection) -> set[str]:
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )
    return {r["name"] for r in cur.fetchall()}


def _guard_select(sql: str) -> str:
    """Allow only a single read statement. Returns the cleaned SQL or raises.

    ``mode=ro`` already blocks every write at the engine level; this is a clear,
    early UX guard so the model gets "read-only" feedback instead of a cryptic
    sqlite OperationalError. Permits SELECT / WITH / EXPLAIN; rejects multiple
    statements.
    """
    cleaned = sql.strip().rstrip(";").strip()
    if not cleaned:
        raise ValueError("Empty SQL.")
    if ";" in cleaned:
        raise ValueError("Only one statement at a time — remove the ';'.")
    head = cleaned.lstrip("(").split(None, 1)[0].lower()
    if head not in ("select", "with", "explain"):
        raise ValueError(
            f"Read-only server: only SELECT / WITH / EXPLAIN are allowed, got "
            f"'{head.upper()}'. (Use describe_table for schema introspection.)"
        )
    return cleaned


# ── Tools ─────────────────────────────────────────────────────────────────


@mcp.tool()
def run_sql(sql: str, limit: int = _DEFAULT_ROWS, params: list[Any] | None = None) -> str:
    """Run a read-only SQL query against the digest's SQLite warehouse.

    This is the analyst's primary instrument: write arbitrary SELECT / WITH /
    EXPLAIN — joins, CTEs, window functions, aggregates — to interrogate the
    data. The connection is read-only, so writes are impossible. Use
    `describe_table` / `schema://overview` to learn the columns first.

    Args:
        sql: a single SELECT, WITH, or EXPLAIN statement (no trailing ';' needed).
        limit: max rows returned, clamped to [1, 2000]. Add your own ORDER BY.
        params: optional positional bind values for '?' placeholders, e.g.
            run_sql("SELECT * FROM items WHERE source = ?", params=["edgar"]).

    Returns JSON: {columns, rows, row_count, truncated, sql}.
    """
    cleaned = _guard_select(sql)
    capped = max(1, min(int(limit), _MAX_ROWS))
    with _ro_conn() as conn:
        cur = conn.execute(cleaned, tuple(params or ()))
        cols = [d[0] for d in cur.description] if cur.description else []
        fetched = cur.fetchmany(capped + 1)
    truncated = len(fetched) > capped
    rows = [dict(r) for r in fetched[:capped]]
    return _json({
        "columns": cols,
        "rows": rows,
        "row_count": len(rows),
        "truncated": truncated,
        "sql": cleaned,
    })


@mcp.tool()
def list_tables() -> str:
    """List every table in the warehouse with its row count, busiest first.

    A fast orientation tool — see what data exists and how much of it before
    drilling in with describe_table / run_sql.
    """
    with _ro_conn() as conn:
        out = []
        for name in sorted(_table_names(conn)):
            (n,) = conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()  # name from sqlite_master, safe
            out.append({"table": name, "rows": int(n)})
    out.sort(key=lambda r: r["rows"], reverse=True)
    return _json(out)


@mcp.tool()
def describe_table(table: str) -> str:
    """Describe one table: columns (name/type/nullable/pk), indexes, sample rows.

    Args:
        table: a table name (see list_tables). Validated against the catalog, so
            this is the safe way to introspect schema (PRAGMA can't be bound).

    Returns JSON: {table, columns, indexes, sample_rows}.
    """
    with _ro_conn() as conn:
        known = _table_names(conn)
        if table not in known:
            raise ValueError(f"Unknown table {table!r}. Known: {sorted(known)}")
        columns = [
            {
                "name": r["name"],
                "type": r["type"],
                "nullable": not r["notnull"],
                "pk": bool(r["pk"]),
                "default": r["dflt_value"],
            }
            for r in conn.execute(f"PRAGMA table_info({table})").fetchall()
        ]
        indexes = [
            {"name": r["name"], "unique": bool(r["unique"])}
            for r in conn.execute(f"PRAGMA index_list({table})").fetchall()
        ]
        sample = _rows(conn.execute(f"SELECT * FROM {table} LIMIT 3"))
    return _json({
        "table": table,
        "columns": columns,
        "indexes": indexes,
        "sample_rows": sample,
    })


@mcp.tool()
def data_overview() -> str:
    """High-level orientation: volume, date span, source/topic mix, funnel, regime.

    Start here on a fresh question. Returns counts by source and topic, the
    triage funnel (pending/keep/drop), how many items have been scored, the
    overall ingest date range, and the current market-cycle × cat-load regime.
    """
    with _ro_conn() as conn:
        (total,) = conn.execute("SELECT COUNT(*) FROM items").fetchone()
        span = conn.execute(
            "SELECT MIN(ingested_at) AS first, MAX(ingested_at) AS last FROM items"
        ).fetchone()
        by_source = _rows(conn.execute(
            "SELECT source, COUNT(*) AS n FROM items GROUP BY source ORDER BY n DESC"
        ))
        by_topic = _rows(conn.execute(
            "SELECT COALESCE(topic,'(none)') AS topic, COUNT(*) AS n FROM items "
            "WHERE triage_decision='keep' GROUP BY topic ORDER BY n DESC"
        ))
        funnel = _rows(conn.execute(
            "SELECT COALESCE(triage_decision,'pending') AS decision, COUNT(*) AS n "
            "FROM items GROUP BY decision ORDER BY n DESC"
        ))
        (scored,) = conn.execute(
            "SELECT COUNT(DISTINCT item_id) FROM signal_scores"
        ).fetchone()
        regime = conn.execute(
            "SELECT as_of, market_cycle, cat_load, multiplier FROM regime_signals "
            "ORDER BY as_of DESC LIMIT 1"
        ).fetchone()
    return _json({
        "total_items": int(total),
        "ingested_first": span["first"],
        "ingested_last": span["last"],
        "items_by_source": by_source,
        "kept_items_by_topic": by_topic,
        "triage_funnel": funnel,
        "items_scored": int(scored),
        "current_regime": dict(regime) if regime else None,
    })


# Factor → human label, in formula order. The leaderboard score is the product
# of these multipliers (see formula://scoring). Kept in sync with
# digest.signals.Score / score_item.
_SCORE_FACTORS: list[tuple[str, str]] = [
    ("source_mult", "channel trust (EDGAR/NHC high → HN low)"),
    ("regime_mult", "market-cycle × cat-load regime"),
    ("topic_relevance", "topic×regime relevance (currently always 1.0)"),
    ("recency", "freshness, 7-day linear decay, floor 0.3"),
    ("llm_judgment", "summarizer materiality, clamped 0.5–1.5"),
    ("topic_boost", "topic-priority emphasis (liability/personal-lines)"),
    ("burden_boost", "regulatory burden intensity (high=1.3)"),
    ("insurer_boost", "priority carrier (PGR/ALL/State Farm = 1.5)"),
    ("inflation_boost", "names a loss-cost inflation driver"),
    ("regulatory_boost", "names a DOI/SERFF/FAIR-Plan action"),
    ("tplf_boost", "litigation funding / nuclear verdict / MDL"),
    ("reserve_boost", "adverse reserve development on a named insurer"),
]


@mcp.tool()
def score_breakdown(item_id: int) -> str:
    """Root-cause an item's leaderboard score: decompose every multiplier.

    Pulls the item plus its latest signal_scores row and explains *why* it
    ranked where it did — which factors lifted the score (>1.0), which dragged
    (<1.0), and which were neutral (1.0) — then reconstructs the product so you
    can see the dominant driver at a glance. The single best tool for "why is
    this item ranked this high/low?".

    Args:
        item_id: items.id of the item to explain.

    Returns JSON: {item, score, tier, computed_at, factors[], reconstructed_product,
    learned_score, lifting[], dragging[]}.
    """
    with _ro_conn() as conn:
        item = conn.execute(
            "SELECT id, source, source_id, title, topic, url, triage_score, "
            "materiality_score, burden_intensity, burden_direction, sub_tags, state, "
            "ingested_at, published_at FROM items WHERE id = ?",
            (item_id,),
        ).fetchone()
        if item is None:
            raise ValueError(f"No item with id {item_id}.")
        score = conn.execute(
            "SELECT * FROM signal_scores WHERE item_id = ? ORDER BY computed_at DESC LIMIT 1",
            (item_id,),
        ).fetchone()

    if score is None:
        return _json({
            "item": dict(item),
            "score": None,
            "note": "Item has no signal_scores row — not yet scored "
                    "(kept+summarized items are scored by `digest signals`).",
        })

    s = dict(score)
    factors, product = [], 1.0
    lifting, dragging = [], []
    for col, label in _SCORE_FACTORS:
        val = s.get(col)
        if val is None:
            continue
        val = float(val)
        product *= val
        entry = {"factor": col, "value": round(val, 4), "meaning": label}
        factors.append(entry)
        if val > 1.0001:
            lifting.append(entry)
        elif val < 0.9999:
            dragging.append(entry)

    lifting.sort(key=lambda e: e["value"], reverse=True)
    dragging.sort(key=lambda e: e["value"])
    return _json({
        "item": dict(item),
        "score": s.get("score"),
        "tier": s.get("tier"),
        "computed_at": s.get("computed_at"),
        "learned_score": s.get("learned_score"),
        "factors": factors,
        "reconstructed_product": round(product, 4),
        "lifting_factors": lifting,
        "dragging_factors": dragging,
    })


@mcp.tool()
def pipeline_health(hours: int = 48) -> str:
    """Pipeline forensics: per-source last run, errors, funnel, summarizer throughput.

    Use to root-cause "why did source X dry up / why are there few summaries":
    shows the latest run status per source (with any error), error runs in the
    window, the triage funnel, ingest volume per source, and summarizer activity
    by backend.

    Args:
        hours: trailing window for volumes / errors / summarizer activity.
    """
    cutoff = f"-{int(hours)} hours"
    with _ro_conn() as conn:
        last_run = _rows(conn.execute(
            "SELECT source, status, items_new, run_at, error FROM run_log "
            "WHERE id IN (SELECT MAX(id) FROM run_log GROUP BY source) "
            "ORDER BY run_at DESC"
        ))
        errors = _rows(conn.execute(
            "SELECT source, run_at, error FROM run_log "
            "WHERE status = 'error' AND run_at >= datetime('now', ?) "
            "ORDER BY run_at DESC LIMIT 50",
            (cutoff,),
        ))
        funnel = _rows(conn.execute(
            "SELECT COALESCE(triage_decision,'pending') AS decision, COUNT(*) AS n "
            "FROM items GROUP BY decision ORDER BY n DESC"
        ))
        ingest_volume = _rows(conn.execute(
            "SELECT source, COUNT(*) AS n FROM items "
            "WHERE ingested_at >= datetime('now', ?) GROUP BY source ORDER BY n DESC",
            (cutoff,),
        ))
        summarizer = _rows(conn.execute(
            "SELECT backend, COUNT(*) AS n, "
            "       SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS errors, "
            "       SUM(input_chars) AS in_chars, SUM(output_chars) AS out_chars "
            "FROM summarizer_log WHERE run_at >= datetime('now', ?) GROUP BY backend",
            (cutoff,),
        ))
    return _json({
        "window_hours": int(hours),
        "latest_run_per_source": last_run,
        "error_runs": errors,
        "triage_funnel": funnel,
        "ingest_volume": ingest_volume,
        "summarizer_activity": summarizer,
    })


@mcp.tool()
def source_quality(days: int = 30) -> str:
    """Which feeds earn their keep: avg/max leaderboard score + count per source.

    Aggregates the latest score per kept+summarized item by source over the
    window — the data behind "is Reddit pulling its weight vs EDGAR?".

    Args:
        days: trailing window over items.ingested_at (0 = all-time).
    """
    clauses = ["i.triage_decision = 'keep'", "i.summary IS NOT NULL"]
    params: list[Any] = []
    if days and days > 0:
        clauses.append("i.ingested_at >= datetime('now', ?)")
        params.append(f"-{int(days)} days")
    where = " AND ".join(clauses)
    sql = f"""
        WITH latest AS (
            SELECT item_id, MAX(computed_at) AS computed_at
            FROM signal_scores GROUP BY item_id
        )
        SELECT i.source AS source, COUNT(*) AS n,
               ROUND(AVG(s.score), 4) AS avg_score,
               ROUND(MAX(s.score), 4) AS max_score,
               ROUND(MIN(s.score), 4) AS min_score
        FROM signal_scores s
        JOIN latest l ON s.item_id = l.item_id AND s.computed_at = l.computed_at
        JOIN items  i ON i.id = s.item_id
        WHERE {where}
        GROUP BY i.source
        ORDER BY avg_score DESC
    """
    with _ro_conn() as conn:
        out = _rows(conn.execute(sql, tuple(params)))
    return _json({"days": int(days), "by_source": out})


@mcp.tool()
def top_signals(limit: int = 10, since_days: int | None = None, source: str | None = None) -> str:
    """Current leaderboard: top-N kept items by latest score, with factor columns.

    Args:
        limit: how many items (clamped to [1, 100]).
        since_days: only items ingested within this many days (None = all-time).
        source: optional source filter (e.g. 'edgar', 'rss', 'reddit').
    """
    capped = max(1, min(int(limit), 100))
    clauses = ["i.triage_decision = 'keep'", "i.summary IS NOT NULL"]
    params: list[Any] = []
    if since_days is not None:
        clauses.append("i.ingested_at >= datetime('now', ?)")
        params.append(f"-{int(since_days)} days")
    if source:
        clauses.append("i.source = ?")
        params.append(source)
    where = " AND ".join(clauses)
    sql = f"""
        WITH latest AS (
            SELECT item_id, MAX(computed_at) AS computed_at
            FROM signal_scores GROUP BY item_id
        )
        SELECT i.id, i.source, i.topic, i.title, i.url,
               s.score, s.tier, s.computed_at
        FROM signal_scores s
        JOIN latest l ON s.item_id = l.item_id AND s.computed_at = l.computed_at
        JOIN items  i ON i.id = s.item_id
        WHERE {where}
        ORDER BY s.score DESC, i.ingested_at DESC
        LIMIT ?
    """
    params.append(capped)
    with _ro_conn() as conn:
        out = _rows(conn.execute(sql, tuple(params)))
    return _json({"limit": capped, "since_days": since_days, "source": source, "items": out})


@mcp.tool()
def fundamentals(insurer: str) -> str:
    """Insurer fundamentals from the 10-K XBRL concept-registry + statutory feeds.

    Cross-dataset headline for one insurer (ticker, e.g. 'PGR'): which datasets
    are populated (premiums, claim_counts=frequency, ibnr, reserve_development,
    investments, combined_ratio, …), total net earned premium, loss-triangle
    coverage by canonical LOB, and the largest prior-year reserve developments.
    Drill into insurer_xbrl_facts via run_sql for the component breakdown, or use
    statutory_facts for the mutuals (State Farm et al.) absent from SEC XBRL.
    """
    from digest import fundamentals as fmod

    return _json(fmod.insurer_fundamentals(insurer))


@mcp.tool()
def return_forecasts(horizon_days: int | None = None, limit: int = 20) -> str:
    """Alpha-engine forecasts: predicted forward excess return per insurer.

    The local returns model (digest forecast train) predicts each insurer's
    forward return vs the IAK benchmark from the digest's own data + signal
    scores. Advisory only — it rides alongside the heuristic leaderboard, never
    feeds it. Returns the latest forecast per ticker plus the model's honest
    walk-forward scorecard (out-of-sample IC, hit-rate, long-short vs baselines)
    so you can judge whether the signal has any edge before trusting a name.

    Args:
        horizon_days: filter to one horizon (e.g. 20); None = all horizons.
        limit: max forecast rows (clamped to [1, 100]).
    """
    capped = max(1, min(int(limit), 100))
    h_clause = "WHERE horizon_days = ?" if horizon_days is not None else ""
    fc_sql = f"""
        WITH latest AS (
            SELECT ticker, horizon_days, MAX(as_of) AS m
            FROM return_forecasts {h_clause} GROUP BY ticker, horizon_days
        )
        SELECT f.ticker, f.as_of, f.horizon_days, f.pred_excess, f.pred_prob, f.model_id
        FROM return_forecasts f
        JOIN latest l ON f.ticker = l.ticker
                     AND f.horizon_days = l.horizon_days AND f.as_of = l.m
        ORDER BY f.pred_excess DESC LIMIT ?
    """
    fc_params = ([horizon_days] if horizon_days is not None else []) + [capped]
    with _ro_conn() as conn:
        forecasts = _rows(conn.execute(fc_sql, tuple(fc_params)))
        model = _rows(conn.execute(
            """SELECT id, trained_at, horizon_days, algo, n_samples, ic, hit_rate,
                      baseline_ic, long_short_ret
               FROM return_models ORDER BY trained_at DESC, id DESC LIMIT 1"""))
    scorecard = model[0] if model else None
    note = None
    if scorecard:
        from digest import alpha
        note = ("model carries signal — positive IC that beats the baselines"
                if alpha.has_edge(scorecard.get("ic"), scorecard.get("baseline_ic"))
                else "model does NOT carry signal (IC not positive / no lift over momentum) — treat forecasts as noise")
    return _json({"horizon_days": horizon_days, "forecasts": forecasts,
                  "scorecard": scorecard, "edge": note})


# ── Resources ─────────────────────────────────────────────────────────────


_DATA_DICTIONARY = """\
# PC Digest warehouse — data dictionary

The digest pipeline is: ingest → triage (keep/drop + topic) → summarize →
score (leaderboard) → publish. Everything lands in one SQLite DB. Tables you'll
use most for insight + root-cause work:

## Core firehose
- **items** — every ingested article/filing/observation, one row each. Key cols:
  `source` (edgar, rss, reddit, hn, nhc, usgs, fred, courtlistener, serff, …),
  `source_id`, `title`, `content`, `url`, `published_at`, `ingested_at`,
  `metadata_json` (per-source JSON: ticker/form for edgar, magnitude/place for
  usgs, series_id/z_score for fred, rate_change_pct/state for serff, …),
  `topic` (17-topic taxonomy), `summary`, `why_it_matters`, `confidence`,
  `triage_decision` (keep/drop/NULL=pending), `triage_score`,
  `materiality_score` (summarizer 0.5–1.5, feeds llm_judgment),
  `burden_direction`/`burden_intensity` (regulatory_rate items),
  `sub_tags` (JSON list, e.g. ["litigation_tplf"]), `state` (2-letter, on
  regulatory items), `sentiment_label`/`sentiment_score`, `cluster_id`,
  `entities_json`.
- **run_log** — one row per ingestor run: source, status (ok/error), items_new,
  duration_ms, error. The forensic trail for "why did a feed stop".
- **summarizer_log** — per-item summarizer cost/latency: backend, duration_ms,
  input_chars, output_chars, status.

## Scoring & calibration
- **signal_scores** — per-item leaderboard history, keyed (item_id, computed_at).
  Columns are the formula factors: source_mult, regime_mult, topic_relevance,
  recency, llm_judgment, topic_boost, burden_boost, insurer_boost,
  inflation_boost, regulatory_boost, tplf_boost, reserve_boost, plus the final
  `score`, `tier` (high/medium/low), and `learned_score`. See formula://scoring.
- **regime_signals** — two-axis regime over time: market_cycle (hard…soft) ×
  cat_load (low_season…post_major_event), with the multipliers and evidence_json.
- **manual_ratings** — the user's 1–5 ratings of items (calibration input).
- **outcome_backtest** — did a top-ranked item actually matter? corroborated +
  which signals fired (followon/edgar/regime/manual/stock_move), per horizon.
- **learned_models** / **learned_scores** — logistic model trained on the factors
  to predict corroboration; rides alongside the heuristic (non-authoritative).

## Insurance "EKG" quant tables (Wave 4 — may be empty until their jobs run)
- **reinsurance_pricing** — priced ROL / ILS-spread series (market_cycle axis).
- **cat_load_nowcast** — federal-disaster / drought velocity (cat_load axis).
- **severity_index** — blended loss-cost severity tape (inflation boost).
- **litigation_pressure** — per-state×sector verdict/TPLF/docket composite.
- **loss_triangles** / **reserving_signals** — chain-ladder adverse-development.
  loss_triangles now spans the top-10 SEC P&C insurers (was PGR-only) and carries
  **canonical_lob** (unified taxonomy) so lines compare across insurers.
- **disclosure_sentiment** — reserve tone read over EDGAR filings.
- **capital_flows** — structured insurtech deal facts.
- **fred_baseline** — FRED anomaly z-score baselines.
- **item_embeddings** — per-item title+summary vectors (semantic neighbours).

## Insurer fundamentals registry (10-K XBRL concept-registry + statutory)
- **insurer_xbrl_facts** — component-level facts for the top-10 SEC P&C insurers,
  one row per (concept × dimensional context). `dataset` ∈ premiums, claim_counts
  (FREQUENCY by accident_year), ibnr, reserve_development, unpaid_claims,
  reinsurance, investment_income/portfolio/gains, aoci, dac, segment_results,
  combined_ratio, triangle. Dims: segment/product/subsegment/accident_year/
  geography/investment_type. value in USD millions (counts raw). Prefer the
  `fundamentals(insurer)` tool for a headline read.
- **statutory_facts** — high-level facts for the big mutuals NOT in SEC XBRL
  (State Farm, USAA, Liberty Mutual, Farmers, American Family): direct premiums
  written + market share by line (free III feed, source='iii'), source-tagged.
  Carries canonical_lob. This is the only window onto the #1 US insurer.

## Joins worth knowing
- signal_scores.item_id → items.id (latest row = MAX(computed_at) per item_id).
- Most quant tables key on (insurer/index/state, …, as_of/observation_date);
  take the latest with a MAX(as_of) CTE.
- EDGAR carrier: json_extract(items.metadata_json,'$.ticker'); FRED series:
  json_extract(items.metadata_json,'$.series_id'); both also carry '$.z_score'
  / '$.magnitude' / '$.rate_change_pct' depending on source.
"""

_SCORING_FORMULA = """\
# Leaderboard scoring formula

Each kept+summarized item gets a score = the PRODUCT of these multipliers
(persisted as columns on signal_scores, latest row per item_id):

    score = source_mult × regime_mult × topic_relevance × recency
          × llm_judgment × topic_boost × burden_boost
          × insurer_boost × inflation_boost × regulatory_boost
          × tplf_boost × reserve_boost

Neutral baseline ≈ 1.0 (average source, neutral regime, fresh, materiality 1.0,
no boosts). An item climbs by stacking real signal. Conviction `tier`: high ≥1.6,
medium ≥0.9, else low (user-tunable).

Factor meanings:
- source_mult        channel trust. edgar/nhc/clipped 1.3; usgs/fred/courtlistener/
                     state_doi/serff/naic 1.2; investor_supp 1.1; trade press/spc/
                     nifc/collision/industry_research 1.0; substack 0.9; reddit 0.7; hn 0.6.
- regime_mult        market_cycle (0.85–1.20) × cat_load (1.00–1.20).
- topic_relevance    reserved hook — currently always 1.0.
- recency            linear decay over 7 days, floored at 0.3.
- llm_judgment       summarizer materiality_score, clamped to 0.5–1.5.
- topic_boost        topic-priority emphasis: social_inflation/commercial_specialty/
                     reserving/supply_chain 1.4; personal_lines 1.3;
                     underwriting_results/distribution/regulatory_rate 1.2.
- burden_boost       regulatory_rate burden_intensity: high 1.3, medium 1.1, low 1.0.
- insurer_boost      max(EDGAR ticker boost, carrier-name boost). PGR/ALL/BRK and
                     names "state farm"/"allstate" = 1.5; TRV/CB 1.3; HIG/AIG 1.2.
- inflation_boost    1.2 when title/summary names a loss-cost driver (auto parts,
                     labor, medical, verdict, severity, …); up to 1.4 if severity tape hot.
- regulatory_boost   1.2 when it names a DOI/SERFF/FAIR-Plan/NAIC action.
- tplf_boost         1.3 when tagged litigation_tplf or naming TPLF/MDL/nuclear verdict
                     (scales up with the litigation-pressure index).
- reserve_boost      adverse reserve development on a named insurer (1.0 until data).

The user-tunable VALUES live in Obsidian `_meta/Scoring Weights.md`; the regex
patterns are code-side. Use score_breakdown(item_id) to decompose any one item.
"""


@mcp.resource("schema://overview")
def schema_overview() -> str:
    """Data dictionary: what every warehouse table holds and how to join them."""
    return _DATA_DICTIONARY


@mcp.resource("formula://scoring")
def scoring_formula() -> str:
    """The leaderboard scoring formula and the meaning of each factor."""
    return _SCORING_FORMULA


# ── Prompt (the analyst persona + methodology) ─────────────────────────────


@mcp.prompt()
def analyst(question: str = "") -> str:
    """Frame an expert P&C actuary / data-scientist session over the warehouse."""
    task = (
        f"\n\nThe Analyst's current question:\n{question}\n"
        if question.strip()
        else "\n\nAwait the Analyst's question, then begin with data_overview.\n"
    )
    return f"""\
You are **the Analyst** — a senior property & casualty actuary and data scientist
embedded in PC Digest, a daily/weekly intelligence pipeline on US P&C insurance.
Your job is to develop genuine INSIGHTS and ROOT-CAUSE understanding of the data
the pipeline ingests, triages, scores, and publishes — explain *why*, with the
rigor of someone who prices the book and sets the reserves.

Domain fluency you reason WITH, not merely about:
- ACTUARIAL: loss reserving (chain-ladder, Bornhuetter-Ferguson, loss-development
  factors, tail factors, IBNR vs case, paid vs incurred triangles by accident
  year × development period, adverse/favorable development); ratemaking
  (pure-premium & loss-ratio methods, frequency × severity trend, credibility &
  on-leveling, indicated rate change, permissible loss ratio); combined / loss /
  expense ratios (accident- vs calendar-year); catastrophe modeling (AAL, PML,
  EP/OEP/AEP curves, return periods, RMS/Verisk/KCC); reinsurance (quota share,
  excess-of-loss, rate-on-line, attachment/retention, ILS/cat bonds, the
  underwriting cycle).
- UNDERWRITING: risk segmentation, GLM pricing (Poisson/Gamma/Tweedie), rating
  variables & class plans, written vs earned premium, unearned-premium reserve,
  rate adequacy, adverse selection, mix-of-business shift, retention/conversion.
- CLAIMS: frequency & severity, reported vs closed counts, closure/reopen rates,
  LAE/ALAE/ULAE, severity tails (lognormal/gamma/Pareto), large-loss capping,
  development lags, subrogation/salvage, litigation rate & attorney
  representation, nuclear verdicts and social inflation feeding severity.
- STATISTICS / DATA SCIENCE: GLMs & exposure offsets, credibility as Bayesian
  shrinkage, robust anomaly detection (z-score / MAD), time-series trend &
  development, regression diagnostics, confounding & Simpson's paradox, base-rate
  reasoning, calibration / AUC / lift / Gini, backtesting discipline, and a sharp
  line between causal and merely correlational claims.

Analytical discipline ON THIS WAREHOUSE:
- NORMALIZE before comparing — rates per exposure / per day, never raw counts
  across unequal-sized sources or windows.
- Separate accident-year from calendar-year framing for reserving signals; read
  loss_triangles / reserving_signals through a chain-ladder lens and flag thin,
  low-credibility triangles instead of over-reading them.
- Treat severity_index / FRED series as loss-cost TREND; decompose frequency vs
  severity wherever the data lets you — `insurer_xbrl_facts` dataset='claim_counts'
  is reported claim counts by accident year (the FREQUENCY half); dataset=
  'combined_ratio'/'premiums' give the combined-ratio-bridge inputs.
- Use the **fundamentals registry** for carrier financials: `fundamentals(ticker)`
  for a headline, `insurer_xbrl_facts` for the component breakdown (top-10 SEC
  insurers, by segment/product/accident-year), and `statutory_facts` for the big
  mutuals absent from SEC XBRL (State Farm/USAA/…). loss_triangles.canonical_lob
  unifies LOBs so you can compare a line ACROSS insurers.
- Interpret every signal through the regime: market_cycle IS the underwriting
  cycle; cat_load IS catastrophe exposure.
- Trace the liability chain end to end: TPLF / nuclear verdicts → social
  inflation → severity trend → reserve adequacy.

Methodology, every time:
1. ORIENT with `data_overview` (or `list_tables`) — volume, span, mix, funnel, regime.
2. READ THE MODEL — pull `schema://overview` and `formula://scoring` so claims
   rest on what columns actually mean; never guess a column's semantics.
3. HYPOTHESIZE, then QUERY with `run_sql` (joins, CTEs, window functions,
   aggregates welcome). One sharp query beats several vague ones.
4. ROOT-CAUSE, don't correlate-and-stop: `score_breakdown` to decompose a
   ranking, `pipeline_health` to separate real signal from an ingest/summarizer
   artifact, `source_quality` / `top_signals` for the baseline, `fundamentals`
   for a carrier's XBRL/statutory financials. Always run the query that would
   DISCONFIRM your hypothesis, and check confounders (exposure, base rate,
   Simpson's paradox, sample size / credibility).
5. QUANTIFY with appropriate uncertainty — cite the query and the numbers; state
   sample size; flag low-credibility cells. Distinguish data gaps (empty table,
   dead feed, NULL column) from real findings.
6. SYNTHESIZE: the insight, its mechanism, your confidence, and the single query
   that would most strengthen or break it.

You cannot write to the warehouse — reads only. If asked to change data, explain
the query/finding that supports it and hand it back to the user.{task}"""


def main() -> None:
    """Console-script entry point — runs the stdio MCP server."""
    mcp.run()


if __name__ == "__main__":
    main()
