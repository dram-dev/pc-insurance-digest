"""CAT-Load Nowcast (EKG Lead 2) — disaster-velocity z-score + cat_load nudge.

Network-free: `run_cat_nowcast` takes an injectable count function, so the
OpenFEMA fetch is stubbed; `compute_cat_load` is driven with explicit counts +
nowcast so the regime axis is exercised without the live detector inputs.
"""
from __future__ import annotations

import math

import pytest

from digest import cat_nowcast, db, regime


def test_escalate_cat_load_is_escalate_only_and_neutral_when_empty():
    # No reading → unchanged.
    assert cat_nowcast.escalate_cat_load("low_season", {}) == "low_season"
    # Mild z → unchanged.
    assert cat_nowcast.escalate_cat_load("low_season", {"declaration_z": 1.0}) == "low_season"
    # Surge → active_season; extreme → post_major_event (legacy z fallback).
    assert cat_nowcast.escalate_cat_load("low_season", {"declaration_z": 2.5}) == "active_season"
    assert cat_nowcast.escalate_cat_load("low_season", {"declaration_z": 3.5}) == "post_major_event"
    # Never lowers an already-higher state.
    assert cat_nowcast.escalate_cat_load("post_major_event", {"declaration_z": 2.5}) == "post_major_event"


def test_escalate_cat_load_prefers_seasonal_tail_p():
    # An unremarkable month (p=0.5) stays put even with a big z alongside —
    # the z was seasonal-blind, the tail p governs when present.
    assert cat_nowcast.escalate_cat_load(
        "low_season", {"declaration_z": 3.5, "declaration_p": 0.5}) == "low_season"
    assert cat_nowcast.escalate_cat_load(
        "low_season", {"declaration_z": 0.0, "declaration_p": 0.02}) == "active_season"
    assert cat_nowcast.escalate_cat_load(
        "low_season", {"declaration_z": 0.0, "declaration_p": 0.001}) == "post_major_event"
    assert cat_nowcast.escalate_cat_load(
        "post_major_event", {"declaration_p": 0.5}) == "post_major_event"  # never lowers


# ── seasonal count model ──────────────────────────────────────────────────


def test_poisson_sf_basics():
    assert cat_nowcast.poisson_sf(0, 3.0) == 1.0
    assert cat_nowcast.poisson_sf(1, 2.0) == pytest.approx(1 - math.exp(-2.0))
    # Far tail is small and monotone in k.
    assert cat_nowcast.poisson_sf(15, 3.0) < cat_nowcast.poisson_sf(10, 3.0) < 0.01


def test_nb_sf_fatter_tail_than_poisson_when_overdispersed():
    # Same mean, var = 4×mean → the NB tail must dominate the Poisson tail.
    assert cat_nowcast.nb_sf(12, 3.0, 12.0) > cat_nowcast.poisson_sf(12, 3.0)


def test_seasonal_tail_p_dispatch_and_gates():
    assert cat_nowcast.seasonal_tail_p([3, 2], 9) is None          # <3 points
    p_pois = cat_nowcast.seasonal_tail_p([3, 3, 3, 3], 9)          # var≈0 → Poisson
    assert p_pois is not None and p_pois < 0.01
    p_nb = cat_nowcast.seasonal_tail_p([1, 1, 2, 9, 2, 3], 9)      # overdispersed → NB
    assert p_nb is not None and p_nb > p_pois                      # fatter tail, less surprise
    # A typical-for-the-season count is not surprising.
    assert cat_nowcast.seasonal_tail_p([5, 6, 4, 7, 5], 5) > 0.4


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


def test_run_cat_nowcast_seasonal_tail_p_full_history(fresh_db):
    # 121 months (~10y): every month has count 3, except the latest — same
    # calendar month, count 25. Seasonally that's a massive surprise.
    months = cat_nowcast._month_starts(121)
    table = {m: 3 for m in months}
    table[months[-1]] = 25
    out = cat_nowcast.run_cat_nowcast(_count_fn=lambda s, e: table[s])
    assert out["months"] == 121
    assert out["tail_p"] is not None and out["tail_p"] < 0.005
    assert out["written"] == 122                       # 121 z rows + 1 tail-p row

    sig = cat_nowcast.nowcast_signal()
    assert sig["declaration_p"] == pytest.approx(out["tail_p"], abs=1e-9)
    # The full chain escalates to post_major_event on the seasonal surprise.
    counts = {"active_nhc": 0, "recent_major_eq": 0, "recent_wildfire": 0}
    state, _ = regime.compute_cat_load(counts=counts, nowcast=sig)
    assert state == "post_major_event"


def test_run_cat_nowcast_typical_month_no_tail_row_alarm(fresh_db):
    # Latest month right at its seasonal norm → p ≈ large, no escalation.
    months = cat_nowcast._month_starts(121)
    table = {m: 3 + (i % 2) for i, m in enumerate(months)}
    out = cat_nowcast.run_cat_nowcast(_count_fn=lambda s, e: table[s])
    assert out["tail_p"] is not None and out["tail_p"] > 0.2
    sig = cat_nowcast.nowcast_signal()
    assert cat_nowcast.escalate_cat_load("low_season", sig) == "low_season"


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
