"""Litigation Pressure Index (EKG Lead 4) — reducer + TPLF-boost scaling.

Network-free: the Marathon/Westfleet components are pending, so these drive the
reducer, the boost scaling, and a run over the live docket-velocity path.
"""
from __future__ import annotations

import json

from digest import db, litigation, signals


def test_compute_pressure_index_bounds_and_weighting():
    assert litigation.compute_pressure_index() == 0.0          # no data
    # All components maxed → ~100.
    full = litigation.compute_pressure_index(
        verdict_count=20, median_award=100_000_000, tplf_commitments=2e9, docket_velocity=10)
    assert 99.0 <= full <= 100.0
    # Verdicts dominate docket volume: same normalized magnitude, bigger weight.
    verdict_only = litigation.compute_pressure_index(verdict_count=10)   # maxed verdict
    docket_only = litigation.compute_pressure_index(docket_velocity=5)   # maxed docket
    assert verdict_only > docket_only


def test_docket_velocity_alone_stays_conservative():
    # Live-only reading (docket velocity maxed, nothing else) is below the boost floor.
    assert litigation.compute_pressure_index(docket_velocity=5) <= 50.0


def test_tplf_pressure_boost_scales_above_floor_only():
    assert litigation.tplf_pressure_boost(1.3, None) == 1.3       # no data
    assert litigation.tplf_pressure_boost(1.3, 40.0) == 1.3       # below floor
    assert litigation.tplf_pressure_boost(1.3, 50.0) == 1.3       # at floor
    assert litigation.tplf_pressure_boost(1.3, 100.0) == 1.5      # max uplift, capped
    mid = litigation.tplf_pressure_boost(1.3, 75.0)
    assert 1.3 < mid < 1.5


def test_run_litigation_writes_national_row(fresh_db):
    out = litigation.run_litigation()
    assert out["written"] == 1
    row = db.latest_litigation_pressure("US", "all")
    assert row is not None
    assert row["verdict_count"] is None                          # scraper component pending
    assert row["docket_velocity"] is not None                    # live component present
    assert litigation.pressure_signal() == row["pressure_index"]


def test_pressure_signal_none_without_data(fresh_db):
    assert litigation.pressure_signal() is None


def _tplf_row(sub_tags=None):
    return {"sub_tags": json.dumps(sub_tags) if sub_tags is not None else None}


def test_litigation_tplf_boost_scales_with_pressure(fresh_db):
    # Tagged item: flat boost when no pressure, scaled up when pressure is hot.
    row = _tplf_row(["litigation_tplf"])
    from collections import UserDict

    class R(UserDict):
        def keys(self): return self.data.keys()
        def __getitem__(self, k): return self.data[k]

    r = R(row)
    assert signals._litigation_tplf_boost(r, "", 1.3, None) == 1.3
    assert signals._litigation_tplf_boost(r, "", 1.3, 100.0) == 1.5
    # No TPLF signal at all → 1.0 regardless of pressure.
    assert signals._litigation_tplf_boost(R(_tplf_row()), "a cyber update", 1.3, 100.0) == 1.0
