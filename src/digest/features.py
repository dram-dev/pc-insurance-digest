"""Feature panel for the alpha engine — one row per (insurer, as-of date).

This is the "ML on the data + signal scores" layer: it turns the warehouse
(per-insurer signal scores, reserving signals, disclosure tone, market regime)
plus the price store into a tabular panel that `alpha.py` learns forward,
benchmark-relative insurer returns from.

The headline correctness property is **no lookahead**: every feature for date
`t` uses only information whose event time is `<= t`. Signal aggregates use the
trailing `(t - window, t]` window; reserving / disclosure / regime use the most
recent reading at-or-before `t` (a `merge_asof` backward join); price controls
use closes `<= t`. The forward return *label* is NOT built here — it lives in
`alpha.py`, so the panel itself can never leak the future.

The math is isolated in `assemble_panel`, which takes plain in-memory inputs so
it can be unit-tested without a database. `build_panel` is the thin wrapper that
reads the warehouse via `db` and the insurer→ticker map from `outcomes`.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from digest import db
from digest.outcomes import match_insurer

logger = logging.getLogger(__name__)

# Topics whose per-window item counts become features. Chosen for hypothesized
# return relevance: liability/reserving/reg signals lead insurer fundamentals;
# cat_event is the obvious near-term shock.
SIGNAL_TOPICS = [
    "social_inflation", "reserving", "regulatory_rate",
    "underwriting_results", "cat_event", "personal_lines",
]

# Trailing window (days) over which item-level signals aggregate into a date.
DEFAULT_SIGNAL_WINDOW = 5
# How far back to build as-of rows by default.
DEFAULT_LOOKBACK_DAYS = 540

# The feature columns the model consumes, in a stable order (the contract that
# alpha.py persists with each model).
FEATURE_COLUMNS = [
    "ret_5d", "ret_20d", "vol_20d", "mom_60d",
    "sig_score_sum", "sig_score_max", "sig_count", "sig_learned_mean",
    "materiality_mean",
    *[f"n_{t}" for t in SIGNAL_TOPICS],
    "reserve_deterioration", "disclosure_adverse",
    "regime_market_mult", "regime_cat_mult",
]


# ── ticker mapping ──────────────────────────────────────────────────────────


def _row_ticker(source: str, source_id: str | None, title: str | None,
                summary: str | None) -> str | None:
    """Insurer ticker for a scored item — EDGAR carries it in source_id, others
    fall back to a name match (mirrors outcomes._item_ticker without a Row)."""
    if source == "edgar":
        return (source_id or "").split(":", 1)[0] or None
    return match_insurer(f"{title or ''} {summary or ''}")


# ── price-derived controls ──────────────────────────────────────────────────


def _price_features(closes: dict[str, float], as_of: str) -> dict[str, float]:
    """Trailing return / vol / momentum from closes at-or-before `as_of`.

    NaN when there isn't enough history — the model handles NaNs natively, and
    NaN is the honest value (we don't impute a fake 0% return)."""
    dates = sorted(d for d in closes if d <= as_of)
    out = {"ret_5d": np.nan, "ret_20d": np.nan, "vol_20d": np.nan, "mom_60d": np.nan}
    if len(dates) < 2:
        return out
    series = np.array([closes[d] for d in dates], dtype=float)
    last = series[-1]

    def _ret(n: int) -> float:
        return float(last / series[-1 - n] - 1.0) if len(series) > n and series[-1 - n] > 0 else np.nan

    out["ret_5d"] = _ret(5)
    out["ret_20d"] = _ret(20)
    out["mom_60d"] = _ret(60)
    if len(series) >= 21:
        window = series[-21:]
        daily = np.diff(window) / window[:-1]
        out["vol_20d"] = float(np.std(daily))
    return out


# ── core assembly (pure, testable) ──────────────────────────────────────────


def assemble_panel(
    tickers: list[str],
    as_of_dates: list[str],
    signals: pd.DataFrame,
    prices: dict[str, dict[str, float]],
    reserving: pd.DataFrame,
    disclosure: pd.DataFrame,
    regime: pd.DataFrame,
    signal_window: int = DEFAULT_SIGNAL_WINDOW,
) -> pd.DataFrame:
    """Build the (ticker, as_of) feature panel from in-memory inputs.

    `signals`     : columns [ticker, event_date, score, learned_score,
                    materiality, topic] — one row per scored, ticker-mapped item.
    `prices`      : {ticker: {date: close}} insurer closes.
    `reserving`   : columns [insurer, as_of, deterioration_pct].
    `disclosure`  : columns [insurer, as_of, adverse_language_score].
    `regime`      : columns [as_of, market_mult, cat_mult] (market-wide).
    All `*_date`/`as_of` are 'YYYY-MM-DD' strings; the join is strictly as-of.
    """
    rows: list[dict] = []
    sig = signals.copy()
    if not sig.empty:
        sig["event_date"] = sig["event_date"].astype(str).str.slice(0, 10)

    # Pre-sort the as-of feeds once for the backward merge.
    res = _prep_asof(reserving, "insurer")
    dis = _prep_asof(disclosure, "insurer")
    reg = _prep_asof(regime, None)

    for ticker in tickers:
        tsig = sig[sig["ticker"] == ticker] if not sig.empty else sig
        closes = prices.get(ticker, {})
        for as_of in as_of_dates:
            row = {"ticker": ticker, "as_of": as_of}
            row.update(_price_features(closes, as_of))
            row.update(_signal_aggregates(tsig, as_of, signal_window))
            row["reserve_deterioration"] = _asof_value(res, ticker, as_of, "deterioration_pct", 0.0)
            row["disclosure_adverse"] = _asof_value(dis, ticker, as_of, "adverse_language_score", 0.0)
            row["regime_market_mult"] = _asof_value(reg, None, as_of, "market_mult", 1.0)
            row["regime_cat_mult"] = _asof_value(reg, None, as_of, "cat_mult", 1.0)
            rows.append(row)

    panel = pd.DataFrame(rows)
    # Guarantee the full, ordered feature contract even if an input was empty.
    for col in FEATURE_COLUMNS:
        if col not in panel.columns:
            panel[col] = np.nan
    return panel[["ticker", "as_of", *FEATURE_COLUMNS]]


def _signal_aggregates(tsig: pd.DataFrame, as_of: str, window: int) -> dict:
    """Aggregate a ticker's items in the trailing (as_of - window, as_of] window."""
    base = {"sig_score_sum": 0.0, "sig_score_max": 0.0, "sig_count": 0.0,
            "sig_learned_mean": np.nan, "materiality_mean": np.nan,
            **{f"n_{t}": 0.0 for t in SIGNAL_TOPICS}}
    if tsig is None or tsig.empty:
        return base
    lo = (datetime.fromisoformat(as_of) - timedelta(days=window)).strftime("%Y-%m-%d")
    win = tsig[(tsig["event_date"] > lo) & (tsig["event_date"] <= as_of)]
    if win.empty:
        return base
    base["sig_score_sum"] = float(win["score"].sum())
    base["sig_score_max"] = float(win["score"].max())
    base["sig_count"] = float(len(win))
    learned = win["learned_score"].dropna()
    base["sig_learned_mean"] = float(learned.mean()) if not learned.empty else np.nan
    mat = win["materiality"].dropna()
    base["materiality_mean"] = float(mat.mean()) if not mat.empty else np.nan
    counts = win["topic"].value_counts()
    for t in SIGNAL_TOPICS:
        base[f"n_{t}"] = float(counts.get(t, 0))
    return base


def _prep_asof(df: pd.DataFrame, key: str | None) -> pd.DataFrame:
    """Normalize an as-of feed: stringify the date to 10 chars, sort ascending."""
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy()
    out["as_of"] = out["as_of"].astype(str).str.slice(0, 10)
    sort_cols = ([key] if key else []) + ["as_of"]
    return out.sort_values(sort_cols).reset_index(drop=True)


def _asof_value(df: pd.DataFrame, key: str | None, as_of: str,
                col: str, default: float) -> float:
    """Most recent `col` value at-or-before `as_of` (for `key` if keyed), else default."""
    if df is None or df.empty or col not in df.columns:
        return default
    sub = df if key is None else df[df.iloc[:, 0] == key]
    sub = sub[sub["as_of"] <= as_of]
    if sub.empty:
        return default
    val = sub.iloc[-1][col]
    return float(val) if pd.notna(val) else default


# ── warehouse wrapper ───────────────────────────────────────────────────────


def _trading_days(prices: dict[str, dict[str, float]], lookback_days: int) -> list[str]:
    """Union of insurer trading days within the lookback window, ascending."""
    all_days = sorted({d for closes in prices.values() for d in closes})
    if not all_days:
        return []
    cutoff = (datetime.fromisoformat(all_days[-1]) - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    return [d for d in all_days if d >= cutoff]


def build_panel(
    tickers: list[str] | None = None,
    as_of_dates: list[str] | None = None,
    signal_window: int = DEFAULT_SIGNAL_WINDOW,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> pd.DataFrame:
    """Read the warehouse + price store and build the as-of feature panel."""
    insurers = tickers or sorted(t for t in db.priced_tickers() if t not in {"IAK", "SPY"})
    prices = {t: db.price_closes(t) for t in insurers}

    # Scored, ticker-mapped items → the signals frame.
    sig_rows = []
    for r in db.scored_items_for_features():
        tk = _row_ticker(r["source"], r["source_id"], r["title"], r["summary"])
        if tk is None or tk not in insurers:
            continue
        sig_rows.append({
            "ticker": tk, "event_date": r["ingested_at"], "score": r["score"],
            "learned_score": r["learned_score"], "materiality": r["materiality_score"],
            "topic": r["topic"],
        })
    signals = pd.DataFrame(sig_rows, columns=[
        "ticker", "event_date", "score", "learned_score", "materiality", "topic"])

    reserving = pd.DataFrame(
        [{"insurer": r["insurer"], "as_of": r["as_of"],
          "deterioration_pct": r["deterioration_pct"]} for r in db.reserving_signals_all()],
        columns=["insurer", "as_of", "deterioration_pct"])
    disclosure = pd.DataFrame(
        [{"insurer": r["insurer"], "as_of": r["as_of"],
          "adverse_language_score": r["adverse_language_score"]}
         for r in db.disclosure_sentiment_all()],
        columns=["insurer", "as_of", "adverse_language_score"])
    regime = pd.DataFrame(
        [{"as_of": r["as_of"], "market_mult": r["market_cycle_mult"],
          "cat_mult": r["cat_load_mult"]} for r in db.regime_signals_all()],
        columns=["as_of", "market_mult", "cat_mult"])

    days = as_of_dates or _trading_days(prices, lookback_days)
    if not days:
        logger.warning("features: no price history — run `digest forecast prices` first")
        return pd.DataFrame(columns=["ticker", "as_of", *FEATURE_COLUMNS])

    return assemble_panel(insurers, days, signals, prices, reserving,
                          disclosure, regime, signal_window=signal_window)
