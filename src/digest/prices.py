"""Daily price store for the alpha engine — insurers + benchmarks.

The outcomes `stock_move` signal already fetches insurer closes from Yahoo
(chart JSON) with a Stooq fallback, but throws them away after computing one σ.
The alpha engine needs a *persisted, backfilled* panel of prices to build
forward (and benchmark-relative) return labels and trailing-vol controls — so
this module reuses that same free fetch (`outcomes.fetch_daily_closes`) and lands
it in the `prices` table via `db.upsert_prices`.

Tickers = the 14 modeled insurers (`INSURER_TICKERS_WAVE1`) plus benchmark
series (`IAK` insurance ETF, `SPY` broad market) used for excess returns. Yahoo's
`range=2y` gives ~2 years of history on the first call, so the store backfills
for free; later runs just upsert the tail (idempotent on (ticker, date)).

Datacenter IPs get rate-limited/challenged by both vendors — like the outcomes
signal, this actually populates from the residential Mac-mini host. A vendor
failure for one ticker is logged and skipped; it never aborts the run.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from digest import db
from digest.config import settings
from digest.outcomes import fetch_daily_closes
from digest.triage import INSURER_TICKERS_WAVE1

logger = logging.getLogger(__name__)

# Seconds to wait between tickers when falling back to the unauthenticated Yahoo/
# Stooq path — rapid back-to-back requests are exactly what trips Yahoo's 429.
# No spacing is needed once a Tiingo token is configured (its own rate limit is
# generous), so the delay is skipped in that case.
_INTER_TICKER_SLEEP = 2.0

# Benchmark symbols for excess-return labels. IAK = iShares U.S. Insurance ETF
# (the natural peer for a P&C name); SPY = broad market control. These trade
# under their own symbols on Yahoo/Stooq, so no override is needed.
BENCHMARKS = {
    "IAK": "iShares U.S. Insurance ETF",
    "SPY": "SPDR S&P 500 ETF",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _rows_for(ticker: str, kind: str, since: str | None) -> list[dict]:
    """Fetch a ticker's closes and shape them into `prices` rows >= `since`."""
    closes = fetch_daily_closes(ticker)
    if not closes:
        logger.warning("prices: no closes for %s (vendor blocked/empty)", ticker)
        return []
    fetched_at = _now_iso()
    rows = []
    for date, close in closes.items():
        if since and date < since:
            continue
        rows.append({
            "ticker": ticker, "date": date, "close": float(close),
            "kind": kind, "source": "yahoo_or_stooq", "fetched_at": fetched_at,
        })
    return rows


def run_prices(tickers: list[str] | None = None, full: bool = False) -> dict:
    """Refresh the price store for the insurer universe + benchmarks.

    `full=True` re-upserts the entire fetched window (≈2y); otherwise only days
    newer than what's already stored for that ticker are written (the tail).
    Returns {'tickers': n, 'rows': m, 'skipped': [tickers with no data]}.
    """
    db.init_db()
    insurers = tickers or sorted(INSURER_TICKERS_WAVE1)
    plan = [(t, "insurer") for t in insurers] + [(b, "benchmark") for b in BENCHMARKS]

    space = _INTER_TICKER_SLEEP if not settings.tiingo_api_token else 0.0
    total_rows = 0
    skipped: list[str] = []
    for i, (ticker, kind) in enumerate(plan):
        if space and i > 0:
            time.sleep(space)
        since = None if full else db.latest_price_date(ticker)
        rows = _rows_for(ticker, kind, since)
        if not rows:
            skipped.append(ticker)
            continue
        total_rows += db.upsert_prices(rows)

    return {"tickers": len(plan), "rows": total_rows, "skipped": skipped}
