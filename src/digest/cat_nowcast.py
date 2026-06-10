"""CAT-Load Nowcast (EKG Lead 2) — federal-disaster velocity → regime.cat_load.

The regime's cat_load axis is otherwise a threshold read on NHC/USGS/NIFC item
counts (`regime.compute_cat_load`), which misses perils those three sources don't
carry — riverine flooding, severe convective outbreaks, ice storms, etc. This
lead adds a *velocity* read on the federal disaster-declaration stream so an
unusual surge in declarations nudges the axis up even when no named storm / M≥6
quake / large wildfire is in the window:

    OpenFEMA DisasterDeclarationsSummaries  (free, no API key)
      → run_cat_nowcast()  → monthly distinct-disaster counts + 12m z-score
                             + SEASONAL TAIL PROBABILITY for the latest month
      → db.upsert_cat_nowcast()  (local mirror of pc_bronze.cat_load_nowcast)
      → nowcast_signal()  → {'declaration_z': float, 'declaration_p': float}
      → regime.compute_cat_load()  (escalate-only nudge)

PR4: the escalation signal is a PER-CALENDAR-MONTH count model, not a z-score.
Monthly declaration counts are small, overdispersed, and strongly seasonal — a
12-month z flags every June against a window containing winter. Instead, the
latest month is compared to the SAME calendar month across ~10y of history:
Poisson when the month's variance ≈ its mean, negative binomial (method of
moments) when overdispersed, and the signal is the tail exceedance probability
P(N ≥ observed). Escalation: p < 0.05 → at least active_season; p < 0.005 →
post_major_event. The z-score is still computed and stored (continuity +
fallback for pre-PR4 stored rows); the tail p is persisted as a parallel
`declaration_tail_p` metric row in the same table.

Behavior-preserving until `digest cat-nowcast` has run: with no stored row,
`nowcast_signal()` returns `{}` and `compute_cat_load` is unchanged.

Databricks-native upgrade: Lakeflow DLT maintains the rolling nowcast and
`ai_forecast()` projects declaration velocity; this local seasonal count model
is the Free-Edition default. US Drought Monitor (USDM) is a second documented
metric for the same table; the OpenFEMA velocity is the v1 signal.
"""
from __future__ import annotations

import logging
import math
import statistics
from datetime import datetime, timezone

import requests

from digest import db

logger = logging.getLogger(__name__)

_OPENFEMA_URL = "https://www.fema.gov/api/open/v2/DisasterDeclarationsSummaries"
_REQUEST_TIMEOUT = 30
_LOOKBACK_MONTHS = 121         # latest month + ~10y of same-calendar-month baseline
_ANOMALY_Z = 2.0               # |z| at/above this flags an anomalous surge
_MIN_SEASONAL_POINTS = 3       # same-calendar-month history needed for a tail p
_MU_FLOOR = 0.25               # count-model mean floor (an all-zero month history
                               # still yields a finite surprise for obs ≥ 1)

# Escalation thresholds for the cat_load nudge (escalate-only, never lower).
# Tail-probability cuts (the PR4 signal):
_ACTIVE_SEASON_P = 0.05        # unusual for the season → at least active_season
_POST_MAJOR_P = 0.005          # extreme for the season → post_major_event
# z fallback (pre-PR4 stored rows that carry no tail p):
_ACTIVE_SEASON_Z = 2.0
_POST_MAJOR_Z = 3.0


# ── Seasonal count model (Poisson / negative-binomial tail) ───────────────


def poisson_sf(k_obs: int, mu: float) -> float:
    """P(N ≥ k_obs) for N ~ Poisson(mu) — iterative pmf, no scipy."""
    if k_obs <= 0:
        return 1.0
    pmf = math.exp(-mu)
    cdf = pmf
    for k in range(1, k_obs):
        pmf *= mu / k
        cdf += pmf
    return max(0.0, min(1.0, 1.0 - cdf))


def nb_sf(k_obs: int, mu: float, var: float) -> float:
    """P(N ≥ k_obs) for an overdispersed count, negative binomial by method of
    moments: r = μ²/(σ²−μ), p = r/(r+μ). Requires var > mu (caller dispatches)."""
    if k_obs <= 0:
        return 1.0
    r = mu * mu / (var - mu)
    p = r / (r + mu)
    pmf = p ** r
    cdf = pmf
    for k in range(1, k_obs):
        pmf *= (k - 1 + r) / k * (1.0 - p)
        cdf += pmf
    return max(0.0, min(1.0, 1.0 - cdf))


def seasonal_tail_p(history: list[int], obs: int) -> float | None:
    """Tail exceedance P(N ≥ obs) vs the same-calendar-month history.

    Poisson when the history's variance ≈ its mean, NB when overdispersed.
    None below _MIN_SEASONAL_POINTS — too thin to define a season."""
    if len(history) < _MIN_SEASONAL_POINTS:
        return None
    mu = max(statistics.fmean(history), _MU_FLOOR)
    var = statistics.pvariance(history)
    if var > mu + 1e-9:
        return nb_sf(obs, mu, var)
    return poisson_sf(obs, mu)


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
    """Fetch trailing monthly disaster counts (~10y) → per-month z rows + a
    seasonal tail probability for the latest month. `_count_fn` is injectable
    for tests; failed months are skipped (warned), so a partial fetch still
    produces a usable tape. Returns a small summary dict."""
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

    # PR4 — seasonal tail p for the latest month: its count vs the SAME
    # calendar month in prior years (Poisson/NB). Stored as a parallel metric
    # row so the fixed cat_load_nowcast schema carries it (value = p).
    latest_month, latest_value = counts[-1]
    season = int(latest_month[5:7])
    history = [v for m, v in counts[:-1] if int(m[5:7]) == season]
    tail_p = seasonal_tail_p(history, latest_value)
    if tail_p is not None:
        rows.append({
            "metric_name": "declaration_tail_p", "region": "US",
            "observation_date": latest_month, "value": round(tail_p, 6),
            "zscore_12m": None, "is_anomaly": int(tail_p < _ACTIVE_SEASON_P),
            "source": "openfema", "fetched_at": fetched_at,
        })

    written = db.upsert_cat_nowcast(rows)
    latest_z = rows[len(counts) - 1]["zscore_12m"]
    logger.info(
        "cat_nowcast: %d months, latest=%d z=%+.2f tail_p=%s (season n=%d)",
        len(counts), latest_value, latest_z,
        f"{tail_p:.4f}" if tail_p is not None else "n/a", len(history),
    )
    return {"months": len(counts), "written": written,
            "anomaly": int(abs(latest_z) >= _ANOMALY_Z),
            "tail_p": tail_p}


def nowcast_signal() -> dict[str, float]:
    """Latest stored disaster-velocity reading for compute_cat_load, or {} when
    no nowcast has run yet (keeps the regime axis behavior-preserving).

    Carries the seasonal tail p when one was stored for the same month —
    escalate_cat_load prefers it; the z is the pre-PR4 fallback."""
    row = db.latest_cat_nowcast("open_disaster_declarations", "US")
    if row is None or row["zscore_12m"] is None:
        return {}
    out = {"declaration_z": float(row["zscore_12m"])}
    prow = db.latest_cat_nowcast("declaration_tail_p", "US")
    if (prow is not None and prow["value"] is not None
            and prow["observation_date"] == row["observation_date"]):
        out["declaration_p"] = float(prow["value"])
    return out


def escalate_cat_load(state: str, nowcast: dict[str, float]) -> str:
    """Escalate-only cat_load nudge. Never lowers the state; no-op when no
    nowcast reading exists.

    Prefers the seasonal tail probability (PR4): p < 0.005 → post_major_event,
    p < 0.05 → active_season — 'how unusual for THIS calendar month', honest
    about count overdispersion. Falls back to the legacy z thresholds for
    stored rows that predate the tail p."""
    order = ["low_season", "active_season", "post_major_event"]
    rank = {s: i for i, s in enumerate(order)}
    proposed = state
    p = nowcast.get("declaration_p")
    if p is not None:
        if p < _POST_MAJOR_P:
            proposed = "post_major_event"
        elif p < _ACTIVE_SEASON_P:
            proposed = "active_season"
    else:
        z = nowcast.get("declaration_z")
        if z is None:
            return state
        if z >= _POST_MAJOR_Z:
            proposed = "post_major_event"
        elif z >= _ACTIVE_SEASON_Z:
            proposed = "active_season"
    return proposed if rank.get(proposed, 0) > rank.get(state, 0) else state
