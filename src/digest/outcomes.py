"""Outcome backtest (Databricks Option 1b) — did a ranked item actually matter?

For each scored, kept item whose horizon (7d and 30d) has elapsed, five detectors
look at the window (t, t+N] and decide whether the item *corroborated*:

  followon    ≥1 later item is semantically near it (embeddings; topic fallback)
  edgar       the insurer it names filed an 8-K/10-Q in-window
  regime      a cat_load/market_cycle shift occurred in-window
  manual      the user later rated it ≥4
  stock_move  the named insurer's return crossed ≥1.0σ (own trailing vol),
              with the σ band recorded granularly (0.5/0.75/.../2+)

corroborated = any signal fired. Results → signal_outcomes (+ silver mirror),
feeding gold.outcome_hit_rate / outcome_by_factor and the Option-4 learned scorer.

Design choices (locked with the user): dual horizon 7+30, binary + which-fired,
Stooq daily closes (no pandas/yfinance), σ vs the stock's own trailing vol.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import math
import re
from datetime import datetime, timedelta

import numpy as np
import requests

from digest import db

logger = logging.getLogger(__name__)

FOLLOWON_THRESHOLD = 0.80     # cosine for "semantically near"
STOCK_TRIGGER_SIGMA = 1.0     # |z| at/above this fires stock_move
MANUAL_GOOD = 4.0             # rating ≥ this corroborates

# Insurer NAME aliases (never bare tickers — "ALL"/"EG" would false-match prose).
# Used to map a non-EDGAR item to a ticker; EDGAR items carry it in source_id.
INSURER_ALIASES: dict[str, list[str]] = {
    "TRV": ["Travelers"],
    "ALL": ["Allstate"],
    "PGR": ["Progressive"],
    "CB":  ["Chubb"],
    "HIG": ["The Hartford", "Hartford Financial", "Hartford"],
    "AIG": ["AIG", "American International"],
    "MET": ["MetLife"],
    "PRU": ["Prudential"],
    "RNR": ["RenaissanceRe", "Renaissance Re", "RenRe"],
    "EG":  ["Everest Group", "Everest Re", "Everest"],
    "AXS": ["AXIS Capital", "Axis Capital"],
    "MMC": ["Marsh McLennan", "Marsh & McLennan", "Marsh"],
    "AON": ["Aon"],
    "WTW": ["Willis Towers Watson", "Willis Towers", "WTW", "Willis"],
    "BRK": ["Berkshire Hathaway", "Berkshire", "GEICO"],
}

# ticker → Stooq symbol (mostly {ticker}.us; BRK trades as class B on Stooq).
_STOOQ_OVERRIDE = {"BRK": "brk-b.us"}


def match_insurer(text: str) -> str | None:
    """First insurer ticker whose name alias appears in `text` (word-boundary,
    case-insensitive). Longest aliases checked first to prefer specific names."""
    if not text:
        return None
    aliases = sorted(
        ((tk, a) for tk, al in INSURER_ALIASES.items() for a in al),
        key=lambda p: len(p[1]), reverse=True,
    )
    for ticker, alias in aliases:
        if re.search(rf"\b{re.escape(alias)}\b", text, re.IGNORECASE):
            return ticker
    return None


# ── Stock-move signal (Stooq daily closes + own-vol σ) ───────────────────


def _stooq_symbol(ticker: str) -> str:
    return _STOOQ_OVERRIDE.get(ticker, f"{ticker.lower()}.us")


def fetch_daily_closes(ticker: str) -> dict[str, float]:
    """{ 'YYYY-MM-DD': close } from Stooq's free CSV. {} on any failure (the
    signal just doesn't fire — never aborts the backtest)."""
    url = f"https://stooq.com/q/d/l/?s={_stooq_symbol(ticker)}&i=d"
    try:
        r = requests.get(url, timeout=30)
        r.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("outcomes: Stooq fetch failed for %s: %s", ticker, exc)
        return {}
    closes: dict[str, float] = {}
    for row in csv.DictReader(io.StringIO(r.text)):
        try:
            closes[row["Date"]] = float(row["Close"])
        except (KeyError, ValueError):
            continue
    return closes


def sigma_band(absz: float) -> str | None:
    """Bucket |z| into the user's bands; None below 0.5σ (no material move)."""
    edges = [2.0, 1.75, 1.5, 1.25, 1.0, 0.75, 0.5]
    labels = {2.0: "2+", 1.75: "1.75-2.0", 1.5: "1.5-1.75", 1.25: "1.25-1.5",
              1.0: "1.0-1.25", 0.75: "0.75-1.0", 0.5: "0.5-0.75"}
    for e in edges:
        if absz >= e:
            return labels[e]
    return None


def compute_stock_move(
    closes: dict[str, float], start_iso: str, horizon_days: int,
) -> tuple[float | None, str | None]:
    """Signed σ of the insurer's return over (t, t+N] vs its own trailing daily
    vol scaled to the horizon. (None, None) if there isn't enough price history."""
    if not closes:
        return None, None
    dates = sorted(closes)
    start = start_iso[:10]
    end = (datetime.fromisoformat(start) + timedelta(days=horizon_days)).strftime("%Y-%m-%d")

    def _on_or_before(d: str) -> str | None:
        prior = [x for x in dates if x <= d]
        return prior[-1] if prior else None

    d0, d1 = _on_or_before(start), _on_or_before(end)
    if not d0 or not d1 or d0 == d1:
        return None, None
    p0, p1 = closes[d0], closes[d1]
    if p0 <= 0:
        return None, None
    horizon_return = (p1 - p0) / p0

    # Trailing daily returns up to d0 → daily σ → horizon σ.
    window = [closes[d] for d in dates if d <= d0][-120:]
    if len(window) < 20:
        return None, None
    arr = np.array(window, dtype=float)
    daily_rets = np.diff(arr) / arr[:-1]
    daily_sigma = float(np.std(daily_rets))
    if daily_sigma <= 0:
        return None, None
    n_trading = sum(1 for d in dates if d0 < d <= d1) or 1
    horizon_sigma = daily_sigma * math.sqrt(n_trading)
    z = horizon_return / horizon_sigma
    return z, sigma_band(abs(z))


# ── Follow-on (semantic recurrence) ──────────────────────────────────────


def _followon_count(
    item: dict, end_iso: str, emb: dict[int, tuple], emb_rows: list,
) -> int:
    """Count later items semantically near `item` within (t, end]. Uses
    embeddings when the item has one; else falls back to same-topic forward count."""
    iid, t = item["id"], item["ingested_at"]
    if iid in emb:
        qvec, _ = emb[iid]
        qn = qvec / (np.linalg.norm(qvec) or 1.0)
        n = 0
        for r in emb_rows:
            if r["item_id"] == iid:
                continue
            ts = str(r["ingested_at"])
            if not (str(t) < ts <= str(end_iso)):
                continue
            v = np.array(json.loads(r["vector_json"]), dtype=float)
            if float(qn @ (v / (np.linalg.norm(v) or 1.0))) >= FOLLOWON_THRESHOLD:
                n += 1
        return n
    return db.forward_topic_count(item["topic"], t, end_iso, iid)


# ── Job ───────────────────────────────────────────────────────────────────


def _regime_shifted(start_iso: str, end_iso: str) -> bool:
    base = db.regime_state_at(start_iso)
    base_state = (base["market_cycle"], base["cat_load"]) if base else None
    for r in db.regime_rows_in_window(start_iso, end_iso):
        if (r["market_cycle"], r["cat_load"]) != base_state:
            return True
    return False


def run_outcomes(horizons: tuple[int, ...] = (7, 30), limit: int = 500) -> dict[int, int]:
    """Score outcomes for matured items at each horizon. Returns {horizon: n_checked}."""
    emb_rows = db.embeddings_with_time()
    emb = {r["item_id"]: (np.array(json.loads(r["vector_json"]), dtype=float),
                          r["ingested_at"]) for r in emb_rows}
    price_cache: dict[str, dict[str, float]] = {}
    checked: dict[int, int] = {}

    for horizon in horizons:
        items = db.items_for_backtest(horizon, limit=limit)
        for it in items:
            t = it["ingested_at"]
            end = (datetime.fromisoformat(str(t)[:19].replace(" ", "T"))
                   + timedelta(days=horizon)).strftime("%Y-%m-%d %H:%M:%S")
            signals: list[str] = []

            followon = _followon_count(it, end, emb, emb_rows)
            if followon > 0:
                signals.append("followon")

            ticker = match_insurer(f"{it['title'] or ''} {it['summary'] or ''}")
            edgar_filed = False
            smz = sband = None
            if ticker:
                edgar_filed = db.edgar_filings_in_window(ticker, t, end) > 0
                if edgar_filed:
                    signals.append("edgar")
                if ticker not in price_cache:
                    price_cache[ticker] = fetch_daily_closes(ticker)
                smz, sband = compute_stock_move(price_cache[ticker], t, horizon)
                if smz is not None and abs(smz) >= STOCK_TRIGGER_SIGMA:
                    signals.append("stock_move")

            regime_shifted = _regime_shifted(t, end)
            if regime_shifted:
                signals.append("regime")

            mr = db.manual_rating_for(it["id"])
            if mr is not None and mr >= MANUAL_GOOD:
                signals.append("manual")

            db.upsert_backtest_outcome(it["id"], horizon, {
                "corroborated":    bool(signals),
                "signals":         signals,
                "followon_count":  followon,
                "edgar_filed":     edgar_filed,
                "regime_shifted":  regime_shifted,
                "manual_rating":   mr,
                "stock_move_z":    smz,
                "stock_move_band": sband,
            })
        checked[horizon] = len(items)
        logger.info("outcomes: horizon=%dd checked=%d", horizon, len(items))
    return checked
