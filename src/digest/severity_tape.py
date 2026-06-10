"""Severity Tape (EKG Lead 3) — blended loss-cost index → inflation boost + trend.

`signals._inflation_keyword_boost` fires a flat 1.2× whenever an item names a
loss-cost driver (auto parts, repair labor, medical, used-car, severity). That's
a *keyword* hit with no sense of *magnitude*. This lead blends the loss-cost FRED
series PC Digest already ingests (`config/fred_series.yaml`) into one severity
tape, so the boost can scale with the actual severity regime:

    FRED parts/labor/used-car/medical series   (already ingested by fred.py)
      → run_severity_tape()  → a monthly LEVEL series per component + a blended,
                               rebased composite, each with a rolling 12m z
      → db.upsert_severity_index()  (local mirror of pc_bronze.severity_index)
      → severity_regime()  → latest blended z      (inflation-boost magnitude)
      → severity-trend-decomposition skill  → fit ln(value) over the level tape

Two consumers, two columns:
  - `value` is the index **level** (FRED observation, or for the blend a
    loss-cost-weighted composite rebased to 100 at a common base month —
    PR4: category weights parts .30 / labor .30 / medical .20 / used .10 /
    property .10, tunable via `severity_weights:` in fred_series.yaml). The
    trend skill fits `ln(value)` over the level series, so it must be positive
    and compounding — not a m/m %.
  - `zscore_12m` is the **rolling** z of the month's m/m % vs its trailing-12m
    distribution (fred.py's per-print semantics). The *latest* row's z is
    identical to the old single-point tape, so `severity_regime()` and the
    inflation boost are behavior-preserving — there's just history behind them.

Behavior-preserving until `digest severity-tape` runs: with no stored blend,
`severity_regime()` returns None and the inflation boost keeps its flat value.

Databricks-native upgrade: Manheim UVVI joins the same table as a `used_vehicle`
component and `ai_forecast()` projects the tape; this numpy-free z-blend over the
existing FRED series (the `fred.py` pattern) is the Free-Edition default.
"""
from __future__ import annotations

import logging
import statistics
from datetime import datetime, timezone

import yaml

from digest import db

logger = logging.getLogger(__name__)

_ANOMALY_Z = 1.5
_TAPE_LOOKBACK_MONTHS = 120   # fetch ~10y of monthly levels for a trend-able tape
_MIN_TAPE_POINTS = 6          # a series needs ≥6 monthly levels to contribute
_Z_MIN_HISTORY = 3            # ≥3 prior m/m points before a rolling z is defined
_Z_TRAILING = 11             # trailing m/m points in the z window (fred.py: last-12 ⧵ latest)
_MAD_CONSISTENCY = 1.4826    # MAD → σ under normality (the robust-z scale factor)

# Map a FRED series id → severity category for the tape (label fallback elsewhere).
_CATEGORY = {
    "CUSR0000SETC": "parts", "PCU33633363": "parts",
    "CUSR0000SETD": "labor",
    "CUSR0000SETA02": "used_vehicle", "CUSR0000SETA01": "used_vehicle",
    "CUSR0000SAH1": "property", "CUSR0000SAM2": "medical",
}

# PR4 — loss-cost-share weights per category. Parts CPI and medical CPI are not
# equally important to a P&C loss dollar; the blend weights components by a
# rough auto/home severity composition instead of equal-weighting. Tunable via
# a top-level `severity_weights:` mapping in config/fred_series.yaml; weights
# are shared equally among the series inside a category, and re-normalized over
# whichever components are present in a given month.
CATEGORY_WEIGHTS_DEFAULT: dict[str, float] = {
    "parts":        0.30,
    "labor":        0.30,
    "medical":      0.20,
    "used_vehicle": 0.10,
    "property":     0.10,
    "other":        0.05,
}


def _level_series(obs: list) -> list[tuple[str, float]]:
    """[(date, level)] for valid monthly observations, oldest first (skips '.')."""
    out: list[tuple[str, float]] = []
    for o in obs:
        raw = o.get("value")
        if raw in (None, ".", ""):
            continue
        try:
            out.append((o.get("date", ""), float(raw)))
        except (TypeError, ValueError):
            continue
    return out


def _rolling_z(
    mom_changes: list[tuple[str, float]], robust: bool = False,
) -> dict[str, float]:
    """{date: z} — each month's m/m % vs its trailing-12m distribution.

    Mirrors fred.py's z for the latest print (history = the up-to-11 m/m points
    before the month), rolled across every month with enough history, so each
    stored row carries its own hotness read. Months without ≥_Z_MIN_HISTORY
    prior points (or a zero-variance window) get no z and stay out of the map.

    PR4 `robust`: median/MAD centering and scale (×1.4826) instead of mean/σ —
    one outlier print in an 11-point window can't inflate the σ and mask the
    next one. Opt-in via `severity_robust_z: true` in fred_series.yaml.
    """
    zmap: dict[str, float] = {}
    for i, (date, pct) in enumerate(mom_changes):
        history = [p for _, p in mom_changes[max(0, i - _Z_TRAILING):i]]
        if len(history) < _Z_MIN_HISTORY:
            continue
        if robust:
            center = statistics.median(history)
            mad = statistics.median(abs(p - center) for p in history)
            scale = _MAD_CONSISTENCY * mad
        else:
            center = statistics.fmean(history)
            scale = statistics.pstdev(history)
        if scale == 0:
            continue
        zmap[date] = (pct - center) / scale
    return zmap


def _blended_rows(
    comp_levels: list[dict[str, float]],
    comp_zs: list[dict[str, float]],
    fetched_at: str,
    comp_weights: list[float] | None = None,
) -> list[dict]:
    """Composite blended tape, ascending by date.

    Components sit on different index bases (parts CPI ≠ medical CPI), so a raw
    average is meaningless — rebase each to 100 at a common base month (the
    latest of the per-series start dates, where every series has data), then
    combine the rebased levels into a LOSS-COST-WEIGHTED composite the trend
    skill can fit (PR4: `comp_weights`, re-normalized over the components
    present each month; None → equal weights, the pre-PR4 blend). The blended z
    is the same weighted mean of component z's. A row is emitted only when at
    least a majority of components are present, so a thin early month can't
    define the blend.
    """
    if not comp_levels:
        return []
    weights = comp_weights or [1.0] * len(comp_levels)
    need = max(1, (len(comp_levels) + 1) // 2)
    base_date = max(min(levels) for levels in comp_levels)   # latest per-series start
    all_dates = sorted({d for levels in comp_levels for d in levels if d >= base_date})

    out: list[dict] = []
    for date in all_dates:
        rebased = [
            (levels[date] / levels[base_date] * 100.0, w)
            for levels, w in zip(comp_levels, weights)
            if levels.get(date) and levels.get(base_date)
        ]
        if len(rebased) < need:
            continue
        wsum = sum(w for _, w in rebased)
        value = sum(v * w for v, w in rebased) / wsum
        zs = [(zmap[date], w) for zmap, w in zip(comp_zs, weights) if date in zmap]
        z = (sum(zv * w for zv, w in zs) / sum(w for _, w in zs)) if zs else None
        out.append({
            "index_name": "blended_severity", "observation_date": date,
            "value": round(value, 4),
            "zscore_12m": round(z, 3) if z is not None else None,
            "is_anomaly": int(z is not None and abs(z) >= _ANOMALY_Z),
            "category": "blended", "source": "fred", "fetched_at": fetched_at,
        })
    return out


def run_severity_tape(_fetch=None) -> dict[str, int]:
    """Backfill the full monthly severity tape: one level row per (series, month)
    plus a rebased, LOSS-COST-WEIGHTED blended composite, each carrying a
    rolling 12m z. `_fetch(series_id) -> observations` is injectable for tests;
    production pulls `_TAPE_LOOKBACK_MONTHS` of history from FRED (needs
    FRED_API_KEY).

    Config (fred_series.yaml, both optional): `severity_weights:` overrides
    CATEGORY_WEIGHTS_DEFAULT per category; `severity_robust_z: true` switches
    the rolling z to median/MAD."""
    from digest.ingest.fred import _CONFIG_PATH, _fetch_series, _mom_pct_changes

    fetch = _fetch or (lambda sid: _fetch_series(sid, limit=_TAPE_LOOKBACK_MONTHS))
    config = yaml.safe_load(_CONFIG_PATH.read_text()) or {}
    series = config.get("series", [])
    cat_weights = {**CATEGORY_WEIGHTS_DEFAULT,
                   **(config.get("severity_weights") or {})}
    robust = bool(config.get("severity_robust_z", False))
    fetched_at = datetime.now(tz=timezone.utc).isoformat()

    rows: list[dict] = []
    comp_levels: list[dict[str, float]] = []
    comp_zs: list[dict[str, float]] = []
    comp_cats: list[str] = []

    for entry in series:
        sid = entry["id"]
        try:
            obs = fetch(sid)
        except Exception as exc:  # noqa: BLE001 — one bad series shouldn't abort
            logger.warning("severity_tape: fetch failed for %s: %s", sid, exc)
            continue
        levels = _level_series(obs)
        if len(levels) < _MIN_TAPE_POINTS:
            continue
        zmap = _rolling_z(_mom_pct_changes(obs), robust=robust)
        category = _CATEGORY.get(sid, "other")
        for date, level in levels:
            z = zmap.get(date)
            rows.append({
                "index_name": f"fred_{sid}", "observation_date": date,
                "value": round(level, 4),
                "zscore_12m": round(z, 3) if z is not None else None,
                "is_anomaly": int(z is not None and abs(z) >= _ANOMALY_Z),
                "category": category, "source": "fred", "fetched_at": fetched_at,
            })
        comp_levels.append(dict(levels))
        comp_zs.append(zmap)
        comp_cats.append(category)

    components = len(comp_levels)
    if components == 0:
        logger.info("severity_tape: no usable FRED series — skipping")
        return {"components": 0, "written": 0}

    # Per-series weight = its category's loss-cost share, split equally among
    # the series inside that category (two parts CPIs share the parts weight).
    cat_n = {c: comp_cats.count(c) for c in set(comp_cats)}
    comp_weights = [
        cat_weights.get(c, cat_weights.get("other", 0.05)) / cat_n[c]
        for c in comp_cats
    ]

    blended = _blended_rows(comp_levels, comp_zs, fetched_at, comp_weights)
    rows.extend(blended)
    written = db.upsert_severity_index(rows)

    latest_z = blended[-1]["zscore_12m"] if blended else None
    logger.info(
        "severity_tape: %d components → %d rows (%d blended), latest blended z=%s",
        components, written, len(blended),
        f"{latest_z:+.2f}" if latest_z is not None else "n/a",
    )
    return {
        "components": components,
        "written": written,
        "anomaly": int(latest_z is not None and abs(latest_z) >= _ANOMALY_Z),
    }


def severity_regime() -> float | None:
    """Latest blended-severity z-score for the inflation boost, or None when the
    tape hasn't run (keeps the boost behavior-preserving)."""
    row = db.latest_severity_index("blended_severity")
    if row is None or row["zscore_12m"] is None:
        return None
    return float(row["zscore_12m"])
