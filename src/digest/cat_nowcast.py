"""CAT-Load Nowcast (EKG Lead 2) — federal-disaster velocity → regime.cat_load.

The regime's cat_load axis is otherwise a threshold read on NHC/USGS/NIFC item
counts (`regime.compute_cat_load`), which misses perils those three sources don't
carry — riverine flooding, severe convective outbreaks, ice storms, etc. This
lead adds a *velocity* read on the federal disaster-declaration stream so an
unusual surge in declarations nudges the axis up even when no named storm / M≥6
quake / large wildfire is in the window:

    OpenFEMA DisasterDeclarationsSummaries  (free, no API key)
      → run_cat_nowcast()  → monthly distinct-disaster counts + 12m z-score
      → db.upsert_cat_nowcast()  (local mirror of pc_bronze.cat_load_nowcast)
      → nowcast_signal()  → {'declaration_z': float|None}
      → regime.compute_cat_load()  (escalate-only nudge)

Behavior-preserving until `digest cat-nowcast` has run: with no stored row,
`nowcast_signal()` returns `{}` and `compute_cat_load` is unchanged.

Databricks-native upgrade: Lakeflow DLT maintains the rolling nowcast and
`ai_forecast()` projects declaration velocity; this local z-score over the
trailing-12m baseline (the `fred.py` pattern) is the Free-Edition default.
US Drought Monitor (USDM) is a second documented metric for the same table; the
OpenFEMA velocity is the v1 signal.
"""
from __future__ import annotations

import logging
import statistics
from datetime import datetime, timezone

import requests

from digest import db

logger = logging.getLogger(__name__)

_OPENFEMA_URL = "https://www.fema.gov/api/open/v2/DisasterDeclarationsSummaries"
_REQUEST_TIMEOUT = 30
_LOOKBACK_MONTHS = 13          # latest month + a trailing-12m baseline
_ANOMALY_Z = 2.0               # |z| at/above this flags an anomalous surge

# Escalation thresholds for the cat_load nudge (escalate-only, never lower).
_ACTIVE_SEASON_Z = 2.0         # surge → at least active_season
_POST_MAJOR_Z = 3.0            # extreme surge → post_major_event


def _month_starts(n: int, now: datetime | None = None) -> list[str]:
    """First-of-month ISO strings for the trailing `n` months, oldest first
    (includes the current month). e.g. ['2025-05-01', …, '2026-05-01']."""
    now = now or datetime.now(tz=timezone.utc)
    y, m = now.year, now.month
    out: list[str] = []
    for _ in range(n):
        out.append(f"{y:04d}-{m:02d}-01")
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    return list(reversed(out))


def _next_month(month_start: str) -> str:
    d = datetime.strptime(month_start, "%Y-%m-%d")
    y, m = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
    return f"{y:04d}-{m:02d}-01"


def _distinct_disaster_count(start: str, end: str) -> int:
    """Distinct federal disaster numbers declared in [start, end).

    Summary rows are per county×program, so one disaster yields many rows; we
    dedupe on disasterNumber to count distinct declared events.
    """
    params = {
        "$filter": f"declarationDate ge '{start}' and declarationDate lt '{end}'",
        "$select": "disasterNumber",
        "$top": "1000",
    }
    r = requests.get(_OPENFEMA_URL, params=params, timeout=_REQUEST_TIMEOUT)
    r.raise_for_status()
    recs = r.json().get("DisasterDeclarationsSummaries", [])
    return len({rec.get("disasterNumber") for rec in recs if rec.get("disasterNumber") is not None})


def run_cat_nowcast(_count_fn=_distinct_disaster_count) -> dict[str, int]:
    """Fetch trailing-13m monthly disaster counts → z-score → store one row per
    month. `_count_fn` is injectable for tests. Returns a small summary dict."""
    months = _month_starts(_LOOKBACK_MONTHS)
    counts: list[tuple[str, int]] = []
    for start in months:
        try:
            counts.append((start, _count_fn(start, _next_month(start))))
        except Exception as exc:  # noqa: BLE001 — one bad month shouldn't abort
            logger.warning("cat_nowcast: count failed for %s: %s", start, exc)
    if len(counts) < 4:
        logger.info("cat_nowcast: only %d monthly points — skipping", len(counts))
        return {"months": len(counts), "written": 0, "anomaly": 0}

    values = [c for _, c in counts]
    baseline = values[:-1]                      # trailing months excluding latest
    mean = statistics.fmean(baseline)
    stdev = statistics.pstdev(baseline)
    fetched_at = datetime.now(tz=timezone.utc).isoformat()
    rows: list[dict] = []
    for month, value in counts:
        # One fixed baseline for every row (not an expanding window) so a quiet
        # early month with a tiny prior sample isn't spuriously flagged.
        z = (value - mean) / stdev if stdev > 0 else 0.0
        rows.append({
            "metric_name": "open_disaster_declarations", "region": "US",
            "observation_date": month, "value": float(value),
            "zscore_12m": round(z, 3), "is_anomaly": int(abs(z) >= _ANOMALY_Z),
            "source": "openfema", "fetched_at": fetched_at,
        })
    written = db.upsert_cat_nowcast(rows)
    latest_z = rows[-1]["zscore_12m"]
    logger.info("cat_nowcast: %d months, latest=%d z=%+.2f (baseline μ=%.1f σ=%.1f)",
                len(counts), values[-1], latest_z, mean, stdev)
    return {"months": len(counts), "written": written,
            "anomaly": int(abs(latest_z) >= _ANOMALY_Z)}


def nowcast_signal() -> dict[str, float]:
    """Latest stored disaster-velocity reading for compute_cat_load, or {} when
    no nowcast has run yet (keeps the regime axis behavior-preserving)."""
    row = db.latest_cat_nowcast("open_disaster_declarations", "US")
    if row is None or row["zscore_12m"] is None:
        return {}
    return {"declaration_z": float(row["zscore_12m"])}


def escalate_cat_load(state: str, nowcast: dict[str, float]) -> str:
    """Escalate-only cat_load nudge from the disaster-velocity z-score. Never
    lowers the state, and is a no-op when no nowcast reading exists."""
    z = nowcast.get("declaration_z")
    if z is None:
        return state
    order = ["low_season", "active_season", "post_major_event"]
    rank = {s: i for i, s in enumerate(order)}
    proposed = state
    if z >= _POST_MAJOR_Z:
        proposed = "post_major_event"
    elif z >= _ACTIVE_SEASON_Z:
        proposed = "active_season"
    return proposed if rank.get(proposed, 0) > rank.get(state, 0) else state
