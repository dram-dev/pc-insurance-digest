"""Litigation Pressure Index (EKG Lead 4) — verdict/TPLF/docket → tplf boost.

`signals._litigation_tplf_boost` fires a flat 1.3× whenever an item is tagged
`litigation_tplf` or names a TPLF / nuclear-verdict signal. That's a binary hit
with no sense of *how hot* the litigation environment is. This lead builds a
composite pressure index so the boost scales with the environment:

    nuclear-verdict counts + median awards (Marathon)   ┐
    disclosed TPLF commitments (Westfleet)              ├─ compute_pressure_index → 0-100
    CourtListener P&C docket velocity (already ingested)┘
      → db.upsert_litigation_pressure()  (local mirror of pc_silver.litigation_pressure)
      → pressure_signal()  → national pressure index
      → signals._litigation_tplf_boost(row, blob, boost_value, pressure)
         (lifts the TPLF boost when pressure is elevated; flat otherwise)

**Source status.** The dominant components — Marathon nuclear-verdict tracker and
the Westfleet TPLF survey — are published reports (scrape/manual), so they ship
disabled pending Mac-mini validation; `run_litigation` computes the one *live*
component (CourtListener docket velocity, already ingested) now. With only
docket velocity the index stays low (the verdict/TPLF weights dominate), so the
boost barely moves until those scrapers land — appropriately conservative. The
reducer and the boost scaling are complete and tested.

Databricks-native upgrade: Vector Search + `ai_query()` to extract award amounts
from verdict/docket text; this numeric composite is the Free-Edition default.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from digest import db

logger = logging.getLogger(__name__)

# Component weights (sum to 100). Nuclear verdicts + median award dominate; TPLF
# commitments next; raw docket velocity is the smallest (volume ≠ severity).
_W_VERDICT = 40.0
_W_AWARD = 25.0
_W_TPLF = 20.0
_W_DOCKET = 15.0

# Reference scales for normalizing each component to [0,1].
_VERDICT_REF = 10.0        # ~10 nuclear verdicts in the window → component maxed
_AWARD_REF = 50_000_000.0  # $50M median award → maxed (the nuclear-verdict anchor)
_TPLF_REF = 1_000_000_000.0  # $1B disclosed TPLF → maxed
_DOCKET_REF = 5.0          # ~5 new P&C dockets/day → maxed

# Boost scaling: above _PRESSURE_FLOOR the flat boost is lifted up to _UPLIFT,
# capped at _BOOST_CAP. Below the floor (or no data) the boost is unchanged.
_PRESSURE_FLOOR = 50.0
_UPLIFT = 0.2
_BOOST_CAP = 1.5


def _norm(value: float | None, ref: float) -> float:
    if value is None or ref <= 0:
        return 0.0
    return max(0.0, min(value / ref, 1.0))


def compute_pressure_index(
    verdict_count: float | None = None,
    median_award: float | None = None,
    tplf_commitments: float | None = None,
    docket_velocity: float | None = None,
) -> float:
    """Composite litigation-pressure index, 0-100. Missing components contribute
    0, so a partial reading (e.g. docket velocity only) yields a low index."""
    score = (
        _W_VERDICT * _norm(verdict_count, _VERDICT_REF)
        + _W_AWARD * _norm(median_award, _AWARD_REF)
        + _W_TPLF * _norm(tplf_commitments, _TPLF_REF)
        + _W_DOCKET * _norm(docket_velocity, _DOCKET_REF)
    )
    return round(score, 2)


def tplf_pressure_boost(boost_value: float, pressure: float | None) -> float:
    """Scale the flat TPLF boost by the pressure index. No-op (returns
    `boost_value`) when pressure is None or below the floor."""
    if pressure is None or pressure <= _PRESSURE_FLOOR:
        return boost_value
    uplift = (pressure - _PRESSURE_FLOOR) / (100.0 - _PRESSURE_FLOOR) * _UPLIFT
    return round(min(boost_value + uplift, _BOOST_CAP), 4)


def run_litigation() -> dict[str, int]:
    """Compute the national litigation-pressure reading from live inputs.

    v1 uses CourtListener docket velocity (already ingested); the Marathon /
    Westfleet verdict + TPLF components are left None until those scrapers are
    validated, so the index stays conservative. Upserts one (US, all) row.
    """
    docket_velocity = db.courtlistener_docket_velocity()
    verdict_count = median_award = tplf_commitments = None   # scraper components (pending)
    pressure = compute_pressure_index(
        verdict_count, median_award, tplf_commitments, docket_velocity,
    )
    db.upsert_litigation_pressure({
        "state": "US", "sector": "all",
        "as_of": datetime.now(tz=timezone.utc).isoformat(),
        "verdict_count": verdict_count, "median_award": median_award,
        "tplf_commitments": tplf_commitments, "docket_velocity": docket_velocity,
        "pressure_index": pressure,
    })
    logger.info("litigation: docket_velocity=%.2f/day → pressure_index=%.1f "
                "(verdict/TPLF components pending scraper validation)",
                docket_velocity, pressure)
    return {"written": 1, "pressure": int(pressure)}


def pressure_signal() -> float | None:
    """Latest national litigation-pressure index for the boost, or None when the
    reading hasn't been computed (keeps the TPLF boost behavior-preserving)."""
    row = db.latest_litigation_pressure("US", "all")
    if row is None or row["pressure_index"] is None:
        return None
    return float(row["pressure_index"])
