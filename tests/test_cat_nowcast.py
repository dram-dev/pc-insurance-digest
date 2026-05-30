"""CAT-Load Nowcast (EKG Lead 2) — disaster-velocity z-score + cat_load nudge.

Network-free: `run_cat_nowcast` takes an injectable count function, so the
OpenFEMA fetch is stubbed; `compute_cat_load` is driven with explicit counts +
nowcast so the regime axis is exercised without the live detector inputs.
"""
from __future__ import annotations

from digest import cat_nowcast, db, regime


def test_escalate_cat_load_is_escalate_only_and_neutral_when_empty():
    # No reading → unchanged.
    assert cat_nowcast.escalate_cat_load("low_season", {}) == "low_season"
    # Mild z → unchanged.
    assert cat_nowcast.escalate_cat_load("low_season", {"declaration_z": 1.0}) == "low_season"
    # Surge → active_season; extreme → post_major_event.
    assert cat_nowcast.escalate_cat_load("low_season", {"declaration_z": 2.5}) == "active_season"
    assert cat_nowcast.escalate_cat_load("low_season", {"declaration_z": 3.5}) == "post_major_event"
    # Never lowers an already-higher state.
    assert cat_nowcast.escalate_cat_load("post_major_event", {"declaration_z": 2.5}) == "post_major_event"


def test_run_cat_nowcast_flags_anomalous_surge(fresh_db, monkeypatch):
    # 13 months: a realistic (non-degenerate) baseline then a sharp spike.
    months = cat_nowcast._month_starts(13)
    values = [2, 3, 1, 4, 2, 3, 2, 5, 3, 2, 4, 3] + [25]
    table = dict(zip(months, values))
    written = cat_nowcast.run_cat_nowcast(_count_fn=lambda s, e: table[s])
    assert written["months"] == 13
    assert written["written"] == 13
    assert written["anomaly"] == 1                       # final month is a clear outlier

    sig = cat_nowcast.nowcast_signal()
    assert sig["declaration_z"] >= 2.0
    row = db.latest_cat_nowcast("open_disaster_declarations", "US")
    assert row["observation_date"] == months[-1]
    assert row["value"] == 25.0


def test_nowcast_signal_empty_without_data(fresh_db):
    assert cat_nowcast.nowcast_signal() == {}


def test_compute_cat_load_unchanged_without_nowcast(fresh_db):
    counts = {"active_nhc": 0, "recent_major_eq": 0, "recent_wildfire": 0}
    state, _ = regime.compute_cat_load(counts=counts, nowcast={})
    assert state == "low_season"


def test_compute_cat_load_escalates_on_surge(fresh_db):
    counts = {"active_nhc": 0, "recent_major_eq": 0, "recent_wildfire": 0}
    state, out = regime.compute_cat_load(counts=counts, nowcast={"declaration_z": 3.5})
    assert state == "post_major_event"
    assert out["declaration_z"] == 3.5


def test_compute_cat_load_nowcast_never_lowers_event_state(fresh_db):
    # A confirmed major EQ keeps post_major_event even if the nowcast is quiet.
    counts = {"active_nhc": 0, "recent_major_eq": 1, "recent_wildfire": 0}
    state, _ = regime.compute_cat_load(counts=counts, nowcast={"declaration_z": -1.0})
    assert state == "post_major_event"
