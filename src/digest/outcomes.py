"""Outcome backtest (Databricks Option 1b) — did a ranked item actually matter?

For each scored, kept item whose horizon (7d and 30d) has elapsed, item-specific
detectors look at the window (t, t+N] and decide whether the item *corroborated*:

  followon    later items are semantically near it (embeddings; topic fallback),
              counted as a signal only when ELEVATED vs the cohort (≥ the run's
              median) — a lone follow-on in a busy feed is noise, not corroboration
  edgar       the insurer it names filed an 8-K/10-Q in-window
  manual      the user later rated it ≥4
  stock_move  the named insurer's return crossed ≥1.0σ (own trailing vol),
              with the σ band recorded granularly (0.5/0.75/.../2+)

corroborated = any of these item-specific signals fired. (A regime shift in the
window is RECORDED but does not corroborate — it's identical for every item in
the window, so counting it made ~all items positive and starved the learned
scorer of negatives.) Results → outcome_backtest (+ silver mirror), feeding
gold.outcome_hit_rate / outcome_by_factor and the Option-4 learned scorer.

Design: dual horizon 7+30, binary + which-fired, σ vs the stock's own trailing
vol. Prices come free from Yahoo (chart JSON) with a Stooq CSV fallback — both
rate-limit datacenter IPs, so the signal populates from a residential host.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import math
import re
import time
from datetime import datetime, timedelta, timezone

import numpy as np
import requests

from digest import db

logger = logging.getLogger(__name__)

FOLLOWON_THRESHOLD = 0.80     # cosine for "semantically near"
STOCK_TRIGGER_SIGMA = 1.0     # |z| at/above this fires stock_move
MANUAL_GOOD = 4.0             # rating ≥ this corroborates
# A lone "another similar item appeared" is weak corroboration in a busy feed —
# the median item has dozens of same-topic follow-ons. Count follow-on as a
# signal only when it's ELEVATED vs the cohort (the median of the run's counts),
# so the corroboration label has real positive/negative balance for the learned
# scorer to train on (otherwise every item corroborates → single-class → no model).
FOLLOWON_PERCENTILE = 0.5

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

# ticker → vendor symbol (BRK trades as class B). Yahoo uses 'BRK-B', Stooq 'brk-b.us'.
_YAHOO_OVERRIDE = {"BRK": "BRK-B"}
_STOOQ_OVERRIDE = {"BRK": "brk-b.us"}
_PRICE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}


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


# Tiingo uses lowercase symbols with a hyphen for share classes.
_TIINGO_OVERRIDE = {"BRK": "brk-b"}
# Yahoo rate-limits unauthenticated rapid requests with 429 even from residential
# IPs, so a single shared cookie+crumb session (built once, reused across tickers)
# plus exponential backoff is what makes the fetch actually populate.
_YAHOO_BACKOFF = (3.0, 8.0)         # seconds slept after the 1st/2nd 429
_yahoo_session: requests.Session | None = None
_yahoo_crumb: str | None = None
# Circuit breaker: once Yahoo exhausts backoff with a 429, the IP is hard-blocked
# for this process — flip this so the remaining tickers skip Yahoo instantly
# instead of each eating the full backoff (≈minutes × 17 tickers otherwise).
_yahoo_blocked = False


def _tiingo_symbol(ticker: str) -> str:
    return _TIINGO_OVERRIDE.get(ticker, ticker.lower())


def _get_yahoo_session() -> tuple[requests.Session, str | None]:
    """Build (once) a Yahoo session carrying consent cookies + a crumb. Yahoo's
    chart endpoint is far more forgiving of cookie+crumb traffic than of bare
    requests; cached module-wide so all tickers in a run share one handshake."""
    global _yahoo_session, _yahoo_crumb
    if _yahoo_session is not None:
        return _yahoo_session, _yahoo_crumb
    s = requests.Session()
    s.headers.update(_PRICE_HEADERS)
    for warm in ("https://fc.yahoo.com/", "https://finance.yahoo.com/"):
        try:
            s.get(warm, timeout=20)           # sets A1/A3 consent cookies (may 404)
        except Exception:  # noqa: BLE001
            pass
    crumb = None
    try:
        r = s.get("https://query1.finance.yahoo.com/v1/test/getcrumb", timeout=20)
        if r.status_code == 200 and r.text and "<" not in r.text:
            crumb = r.text.strip()
    except Exception:  # noqa: BLE001
        pass
    _yahoo_session, _yahoo_crumb = s, crumb
    return s, crumb


def _fetch_yahoo(ticker: str) -> dict[str, float]:
    """{ 'YYYY-MM-DD': close } from the Yahoo chart JSON API via a cookie+crumb
    session, retrying with backoff on 429 and falling back query1→query2. Raises
    on non-429 HTTP errors; returns {} only if every host/retry is exhausted."""
    global _yahoo_blocked
    if _yahoo_blocked:                       # circuit open — don't retry a hard-blocked IP
        return {}
    symbol = _YAHOO_OVERRIDE.get(ticker, ticker)
    session, crumb = _get_yahoo_session()
    crumb_q = f"&crumb={requests.utils.quote(crumb)}" if crumb else ""
    last_exc: Exception | None = None
    for host in ("query1", "query2"):
        url = (f"https://{host}.finance.yahoo.com/v8/finance/chart/{symbol}"
               f"?range=2y&interval=1d{crumb_q}")
        for attempt, wait in enumerate((*_YAHOO_BACKOFF, None)):
            r = session.get(url, timeout=20)
            if r.status_code == 429 and wait is not None:
                logger.info("outcomes: yahoo 429 for %s (%s, try %d) — backing off %.0fs",
                            ticker, host, attempt + 1, wait)
                time.sleep(wait)
                continue
            if r.status_code == 429:         # exhausted backoff on this host
                last_exc = requests.HTTPError("429 (backoff exhausted)")
                break
            try:
                r.raise_for_status()
            except Exception as exc:  # noqa: BLE001 — try the next host
                last_exc = exc
                break
            res = r.json()["chart"]["result"][0]
            ts = res["timestamp"]
            closes_list = res["indicators"]["quote"][0]["close"]
            out: dict[str, float] = {}
            for epoch, close in zip(ts, closes_list):
                if close is None:
                    continue
                out[datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d")] = float(close)
            return out
    # Both hosts failed. If it was a 429, the IP is throttled — open the breaker
    # so the rest of the run skips Yahoo instead of re-eating the backoff.
    if last_exc is not None and "429" in str(last_exc):
        _yahoo_blocked = True
        logger.warning("outcomes: yahoo 429-blocking this IP — skipping Yahoo for the rest of the run")
    if last_exc:
        raise last_exc
    return {}


def _fetch_tiingo(ticker: str) -> dict[str, float]:
    """{ 'YYYY-MM-DD': adjClose } from Tiingo's free EOD API. The reliable source
    when TIINGO_API_TOKEN is set (no anti-bot games). {} when no token; raises on
    a real HTTP error so the caller falls through to Yahoo/Stooq."""
    from digest.config import settings

    token = settings.tiingo_api_token
    if not token:
        return {}
    url = (f"https://api.tiingo.com/tiingo/daily/{_tiingo_symbol(ticker)}/prices"
           f"?startDate=2024-01-01&token={token}")
    r = requests.get(url, headers={"Content-Type": "application/json"}, timeout=20)
    r.raise_for_status()
    closes: dict[str, float] = {}
    for row in r.json():
        try:
            closes[row["date"][:10]] = float(row.get("adjClose") or row["close"])
        except (KeyError, TypeError, ValueError):
            continue
    return closes


def _fetch_stooq(ticker: str) -> dict[str, float]:
    """{ 'YYYY-MM-DD': close } from Stooq's free CSV. Raises on failure; returns
    {} if the response isn't CSV (Stooq now serves a JS challenge to some IPs)."""
    url = f"https://stooq.com/q/d/l/?s={_stooq_symbol(ticker)}&i=d"
    r = requests.get(url, headers=_PRICE_HEADERS, timeout=20)
    r.raise_for_status()
    closes: dict[str, float] = {}
    for row in csv.DictReader(io.StringIO(r.text)):
        try:
            closes[row["Date"]] = float(row["Close"])
        except (KeyError, ValueError):
            continue
    return closes


def fetch_daily_closes(ticker: str) -> dict[str, float]:
    """{ 'YYYY-MM-DD': close } for a ticker. Source order: Tiingo (if a token is
    set — the reliable path), then Yahoo (cookie+crumb session w/ 429 backoff),
    then Stooq. {} on total failure (the signal just doesn't fire — never aborts
    the backtest). Both unauthenticated vendors throttle/challenge datacenter and
    rapid traffic, so set TIINGO_API_TOKEN for dependable backfills."""
    for source, fetch in (("tiingo", _fetch_tiingo),
                          ("yahoo", _fetch_yahoo),
                          ("stooq", _fetch_stooq)):
        try:
            closes = fetch(ticker)
            if closes:
                return closes
        except Exception as exc:  # noqa: BLE001 — any vendor hiccup → try the next
            logger.warning("outcomes: %s fetch failed for %s: %s", source, ticker, exc)
    return {}


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


def _item_ticker(item) -> str | None:
    """The insurer ticker for an item. EDGAR items carry it deterministically in
    source_id ('PGR:accession') — their title ('PGR 8-K filed …') has no name
    alias for match_insurer to catch, so the edgar/stock signals would otherwise
    never fire for the highest-trust source. Other sources fall back to a name
    match over title + summary."""
    if item["source"] == "edgar":
        return ((item["source_id"] or "").split(":", 1)[0] or None)
    return match_insurer(f"{item['title'] or ''} {item['summary'] or ''}")


def _window_end(t, horizon: int) -> str:
    return (datetime.fromisoformat(str(t)[:19].replace(" ", "T"))
            + timedelta(days=horizon)).strftime("%Y-%m-%d %H:%M:%S")


def _percentile(values, q: float) -> float:
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return 0.0
    return float(vals[min(len(vals) - 1, int(q * len(vals)))])


def _closes_for(ticker: str) -> dict[str, float]:
    """Closes for the σ label — read the persisted price store first
    (db.price_closes), fall back to a live fetch on a miss. The store is the
    backfilled, reproducible source; the live fetch keeps the backtest working
    before `digest forecast prices` has ever run."""
    stored = db.price_closes(ticker)
    if stored:
        return stored
    return fetch_daily_closes(ticker)


def run_outcomes(horizons: tuple[int, ...] = (7, 30), limit: int = 500) -> dict[int, int]:
    """Score outcomes for matured items at each horizon. Returns {horizon: n_checked}."""
    emb_rows = db.embeddings_with_time()
    emb = {r["item_id"]: (np.array(json.loads(r["vector_json"]), dtype=float),
                          r["ingested_at"]) for r in emb_rows}
    price_cache: dict[str, dict[str, float]] = {}
    checked: dict[int, int] = {}

    for horizon in horizons:
        items = db.items_for_backtest(horizon, limit=limit)
        ends = {it["id"]: _window_end(it["ingested_at"], horizon) for it in items}
        # Pass 1: follow-on counts for the cohort → the "elevated" threshold.
        fcounts = {it["id"]: _followon_count(it, ends[it["id"]], emb, emb_rows)
                   for it in items}
        fthresh = max(1.0, _percentile(fcounts.values(), FOLLOWON_PERCENTILE))

        # Pass 2: label each item against the cohort threshold.
        for it in items:
            t, end = it["ingested_at"], ends[it["id"]]
            fc = fcounts[it["id"]]
            signals: list[str] = []
            if fc >= fthresh:                       # elevated, not just ≥1
                signals.append("followon")

            ticker = _item_ticker(it)
            edgar_filed = False
            smz = sband = None
            if ticker:
                edgar_filed = db.edgar_filings_in_window(ticker, t, end) > 0
                if edgar_filed:
                    signals.append("edgar")
                if ticker not in price_cache:
                    price_cache[ticker] = _closes_for(ticker)
                smz, sband = compute_stock_move(price_cache[ticker], t, horizon)
                if smz is not None and abs(smz) >= STOCK_TRIGGER_SIGMA:
                    signals.append("stock_move")

            mr = db.manual_rating_for(it["id"])
            if mr is not None and mr >= MANUAL_GOOD:
                signals.append("manual")

            # Regime shift is a WINDOW property — identical for every item in the
            # window — so it's recorded but does NOT corroborate an individual item
            # (counting it made ~all items positive and broke label balance).
            regime_shifted = _regime_shifted(t, end)

            db.upsert_backtest_outcome(it["id"], horizon, {
                "corroborated":    bool(signals),
                "signals":         signals,
                "followon_count":  fc,
                "edgar_filed":     edgar_filed,
                "regime_shifted":  regime_shifted,
                "manual_rating":   mr,
                "stock_move_z":    smz,
                "stock_move_band": sband,
            })
        checked[horizon] = len(items)
        logger.info("outcomes: horizon=%dd checked=%d (followon≥%.0f)",
                    horizon, len(items), fthresh)
    return checked
