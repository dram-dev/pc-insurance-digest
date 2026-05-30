"""Reinsurance Pulse (EKG Lead 1) — series reducer + market_cycle hook.

Network-free: the GuyCarp/Artemis/Lane scrapers are scaffolds (`enabled: false`),
so these drive the reducer and the regime hook directly.
"""
from __future__ import annotations

from digest import db, reinsurance


def test_reduce_series_detects_firming_and_zscore():
    # A realistic (non-degenerate) baseline near 100 then a sharp step up.
    base = [99, 101, 100, 102, 98, 101, 100, 99, 102, 100, 101]
    obs = [(f"2025-{m:02d}-01", float(v)) for m, v in enumerate(base, start=1)]
    obs.append(("2025-12-01", 140.0))
    out = reinsurance.reduce_series(obs)
    assert out["trend"] == "firming"
    assert out["zscore_12m"] > 1.5
    assert out["is_anomaly"] == 1
    assert out["value"] == 140.0
    assert out["observation_date"] == "2025-12-01"


def test_reduce_series_detects_softening():
    base = [99, 101, 100, 102, 98, 101, 100, 99, 102, 100, 101]
    obs = [(f"2025-{m:02d}-01", float(v)) for m, v in enumerate(base, start=1)]
    obs.append(("2025-12-01", 80.0))
    assert reinsurance.reduce_series(obs)["trend"] == "softening"


def test_reduce_series_too_few_points():
    assert reinsurance.reduce_series([("2025-01-01", 100.0)]) is None


def test_run_reinsurance_noop_without_enabled_sources(fresh_db):
    # All config indices ship enabled:false → clean no-op.
    assert reinsurance.run_reinsurance() == {"indices": 0, "written": 0}


def test_pricing_signal_empty_without_data(fresh_db):
    assert reinsurance.pricing_signal() == {}


def test_market_cycle_hint_firm_only():
    assert reinsurance.market_cycle_hint({"rol_z": 2.5, "trend": "firming"}) == "hard_market"
    assert reinsurance.market_cycle_hint({"rol_z": 1.2, "trend": "firming"}) == "transitioning_to_hard"
    assert reinsurance.market_cycle_hint({"rol_z": 0.5, "trend": "firming"}) is None
    # Softening priced moves never nudge the cycle here.
    assert reinsurance.market_cycle_hint({"rol_z": 3.0, "trend": "softening"}) is None
    assert reinsurance.market_cycle_hint({}) is None


def test_apply_pricing_hint_takes_the_firmer_call(fresh_db):
    from digest import regime
    # Priced firming upgrades a softer LLM call …
    db.upsert_reinsurance_pricing([{
        "index_name": "guycarp_us_property_cat_rol", "observation_date": "2026-01-01",
        "value": 250.0, "zscore_12m": 2.5, "trend": "firming", "is_anomaly": 1,
        "segment": "us_property_cat", "source": "guycarp", "fetched_at": "2026-01-01",
    }])
    assert regime._apply_pricing_hint("stable") == "hard_market"
    # … but never softens an already-harder LLM call.
    assert regime._apply_pricing_hint("hard_market") == "hard_market"


def test_apply_pricing_hint_neutral_without_data(fresh_db):
    from digest import regime
    assert regime._apply_pricing_hint("stable") == "stable"
