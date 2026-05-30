"""Severity Tape (EKG Lead 3) — blended loss-cost index → inflation boost.

`signals._inflation_keyword_boost` fires a flat 1.2× whenever an item names a
loss-cost driver (auto parts, repair labor, medical, used-car, severity). That's
a *keyword* hit with no sense of *magnitude*. This lead blends the loss-cost FRED
series PC Digest already ingests (`config/fred_series.yaml`) into one severity
tape, so the boost can scale with the actual severity regime:

    FRED parts/labor/used-car/medical series   (already ingested by fred.py)
      → run_severity_tape()  → per-series m/m z-score + a blended z
      → db.upsert_severity_index()  (local mirror of pc_bronze.severity_index)
      → severity_regime()  → blended z
      → signals._inflation_keyword_boost(blob, boost_value, severity_z)
         (lifts the keyword boost when the tape is hot; unchanged otherwise)

Behavior-preserving until `digest severity-tape` runs: with no stored blend,
`severity_regime()` returns None and the inflation boost keeps its flat value.

Databricks-native upgrade: Manheim UVVI joins the same table as a `used_vehicle`
component and `ai_forecast()` projects the tape; this numpy z-blend over the
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

# Map a FRED series id → severity category for the tape (label fallback elsewhere).
_CATEGORY = {
    "CUSR0000SETC": "parts", "PCU33633363": "parts",
    "CUSR0000SETD": "labor",
    "CUSR0000SETA02": "used_vehicle", "CUSR0000SETA01": "used_vehicle",
    "CUSR0000SAH1": "property", "CUSR0000SAM2": "medical",
}


def _series_z(obs: list, mom_changes) -> tuple[str, float, float] | None:
    """(latest_date, latest_mom_pct, z) for one FRED series, or None if too short.
    Mirrors fred.py's z math: latest m/m % vs the trailing-12m m/m distribution."""
    changes = mom_changes(obs)
    if len(changes) < 6:
        return None
    window = [pct for _, pct in changes[-12:]]
    latest_date, latest_pct = changes[-1]
    history = window[:-1] if len(window) > 1 else window
    if len(history) < 3:
        return None
    mean = statistics.fmean(history)
    stdev = statistics.pstdev(history)
    if stdev == 0:
        return None
    return latest_date, latest_pct, (latest_pct - mean) / stdev


def run_severity_tape(_fetch=None) -> dict[str, int]:
    """Compute per-series + blended severity z over the FRED loss-cost series and
    upsert severity_index rows. `_fetch(series_id) -> observations` is injectable
    for tests; defaults to the live FRED fetch (needs FRED_API_KEY)."""
    from digest.ingest.fred import _CONFIG_PATH, _fetch_series, _mom_pct_changes

    fetch = _fetch or _fetch_series
    series = (yaml.safe_load(_CONFIG_PATH.read_text()) or {}).get("series", [])
    fetched_at = datetime.now(tz=timezone.utc).isoformat()
    rows: list[dict] = []
    component_z: list[float] = []
    latest_date = ""
    for entry in series:
        sid = entry["id"]
        try:
            res = _series_z(fetch(sid), _mom_pct_changes)
        except Exception as exc:  # noqa: BLE001
            logger.warning("severity_tape: fetch failed for %s: %s", sid, exc)
            continue
        if res is None:
            continue
        date, pct, z = res
        latest_date = max(latest_date, date)
        component_z.append(z)
        rows.append({
            "index_name": f"fred_{sid}", "observation_date": date,
            "value": round(pct, 3), "zscore_12m": round(z, 3),
            "is_anomaly": int(abs(z) >= _ANOMALY_Z),
            "category": _CATEGORY.get(sid, "other"), "source": "fred",
            "fetched_at": fetched_at,
        })
    if not component_z:
        logger.info("severity_tape: no usable FRED series — skipping")
        return {"components": 0, "written": 0}

    blended = statistics.fmean(component_z)
    rows.append({
        "index_name": "blended_severity", "observation_date": latest_date,
        "value": round(blended, 3), "zscore_12m": round(blended, 3),
        "is_anomaly": int(abs(blended) >= _ANOMALY_Z),
        "category": "blended", "source": "fred", "fetched_at": fetched_at,
    })
    written = db.upsert_severity_index(rows)
    logger.info("severity_tape: %d components, blended z=%+.2f", len(component_z), blended)
    return {"components": len(component_z), "written": written,
            "anomaly": int(abs(blended) >= _ANOMALY_Z)}


def severity_regime() -> float | None:
    """Latest blended-severity z-score for the inflation boost, or None when the
    tape hasn't run (keeps the boost behavior-preserving)."""
    row = db.latest_severity_index("blended_severity")
    if row is None or row["zscore_12m"] is None:
        return None
    return float(row["zscore_12m"])
