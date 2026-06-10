"""Reinsurance Pulse (EKG Lead 1) — series reducer + Artemis ROL fetcher + hook.

Network-free: `requests.get`/`_load_config` are monkeypatched, so the fetcher,
reducer, and regime hook are all exercised without touching the live source.
"""
from __future__ import annotations

from digest import db, reinsurance


# Artemis ROL page shape: the series lives in an inline Highcharts config — a
# years `categories` array paired with a numeric `data` array. A decoy chart of
# a different length must be ignored.
_ROL_HTML = """
<script>Highcharts.chart('c', {
  xAxis: { categories: ['2021', '2022', '2023', '2024', '2025', '2026*'] },
  series: [{ name: 'US Property Cat ROL', data: [150, 167.9, 205.1, 276.9, 261.23, 224.65] }]
});</script>
<script>Highcharts.chart('decoy', { series: [{ data: [1, 2, 3] }] });</script>
"""


def _resp(html):
    return type("R", (), {"text": html, "raise_for_status": lambda self: None})()


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


def test_parse_highcharts_year_series_strips_prelim_star_and_ignores_decoy():
    s = reinsurance._parse_highcharts_year_series(_ROL_HTML)
    assert len(s) == 6                      # the decoy (len 3) is not matched
    assert s[0] == ("2021", 150.0)
    assert s[-1] == ("2026", 224.65)        # trailing '*' stripped from 2026*


def test_fetch_series_artemis_rol_dispatch(monkeypatch):
    monkeypatch.setattr(reinsurance.requests, "get", lambda *a, **k: _resp(_ROL_HTML))
    entry = {"index_name": "guycarp_us_property_cat_rol", "source": "artemis_rol",
             "url": "https://www.artemis.bm/us-property-cat-rate-on-line-index/"}
    series = reinsurance._fetch_series(entry)
    assert series[0] == ("2021-01-01", 150.0)      # year → Jan-1 ISO date
    assert series[-1] == ("2026-01-01", 224.65)


def test_fetch_series_unknown_source_is_noop():
    # guycarp commentary / lane PDFs have no registered fetcher yet.
    assert reinsurance._fetch_series({"index_name": "x", "source": "lane"}) == []


def test_run_reinsurance_writes_enabled_artemis_index(fresh_db, monkeypatch):
    monkeypatch.setattr(reinsurance, "_load_config", lambda: [
        {"index_name": "guycarp_us_property_cat_rol", "segment": "us_property_cat",
         "source": "artemis_rol", "url": "https://x/", "enabled": True}])
    monkeypatch.setattr(reinsurance.requests, "get", lambda *a, **k: _resp(_ROL_HTML))
    assert reinsurance.run_reinsurance() == {"indices": 1, "written": 1}
    ps = reinsurance.pricing_signal()
    assert ps["index_name"] == "guycarp_us_property_cat_rol"
    assert ps["trend"] == "firming"         # latest 224.65 > trailing baseline mean


def test_run_reinsurance_noop_without_enabled_sources(fresh_db, monkeypatch):
    # Config-independent: force all indices disabled → clean no-op.
    monkeypatch.setattr(reinsurance, "_load_config",
                        lambda: [{"index_name": "x", "source": "artemis_rol", "enabled": False}])
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


def test_compute_regime_collects_pricing_hint_observation(fresh_db, monkeypatch):
    # PR4: the priced hint is an OBSERVATION for the Markov-switching filter,
    # not a max() against the LLM call. With priced firming stored (and the LLM
    # stubbed to an unobserved fallback), compute_regime must feed the hint to
    # the filter — shifting the posterior without flipping the mode on one read.
    from digest import regime

    db.upsert_reinsurance_pricing([{
        "index_name": "guycarp_us_property_cat_rol", "observation_date": "2026-01-01",
        "value": 250.0, "zscore_12m": 2.5, "trend": "firming", "is_anomaly": 1,
        "segment": "us_property_cat", "source": "guycarp", "fetched_at": "2026-01-01",
    }])
    monkeypatch.setattr(regime, "compute_market_cycle", lambda window_days=60: {
        "market_cycle": "stable", "observed": False, "n_items": 0,
        "combined_ratio_dir": "stable", "capacity_tone": "balanced",
        "evidence": "insufficient evidence",
    })
    monkeypatch.setattr(regime, "compute_cat_load",
                        lambda: ("low_season", {"active_nhc": 0}))
    sig = regime.compute_regime(force=True)
    assert sig.evidence["observations"] == ["hard_market"]
    assert sig.market_cycle == "stable"               # one priced read ≠ a flip
    assert 1.0 < sig.market_cycle_mult < 1.20         # but the multiplier glides up


def test_compute_regime_no_hint_without_pricing_data(fresh_db, monkeypatch):
    from digest import regime

    monkeypatch.setattr(regime, "compute_market_cycle", lambda window_days=60: {
        "market_cycle": "stable", "observed": False, "n_items": 0,
        "combined_ratio_dir": "stable", "capacity_tone": "balanced",
        "evidence": "insufficient evidence",
    })
    monkeypatch.setattr(regime, "compute_cat_load",
                        lambda: ("low_season", {"active_nhc": 0}))
    sig = regime.compute_regime(force=True)
    assert sig.evidence["observations"] == []         # pure predict step
    assert sig.market_cycle == "stable"
