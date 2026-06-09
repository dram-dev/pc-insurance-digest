"""Alpha engine Phase 3 — labels, metrics, purged walk-forward, train/predict."""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from digest import alpha, db, features


# ── metrics ──────────────────────────────────────────────────────────────


def test_information_coefficient_perfect_and_inverse():
    a = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
    assert alpha.information_coefficient(a, a) == pytest.approx(1.0)        # perfect rank match
    assert alpha.information_coefficient(a, a[::-1]) == pytest.approx(-1.0)  # perfectly inverted


def test_information_coefficient_guards_degenerate():
    assert alpha.information_coefficient(np.array([1.0]), np.array([1.0])) is None
    flat = np.array([2.0, 2.0, 2.0, 2.0])
    assert alpha.information_coefficient(flat, np.array([1.0, 2.0, 3.0, 4.0])) is None


def test_hit_rate_and_long_short():
    pred = np.array([0.1, -0.1, 0.2, -0.2])
    actual = np.array([0.05, -0.3, 0.4, 0.01])   # signs match on 3 of 4
    assert alpha.hit_rate(pred, actual) == 0.75
    # top-1 (pred .2 → actual .4) minus bottom-1 (pred -.2 → actual .01)
    assert alpha.long_short_return(pred, actual, q=0.25) == 0.4 - 0.01


# ── labels: strictly forward-looking ───────────────────────────────────────


def test_add_labels_excess_is_forward_and_benchmark_relative():
    closes = {f"2026-06-{d:02d}": 100.0 + d for d in range(1, 21)}  # +1/day
    bench = {f"2026-06-{d:02d}": 200.0 for d in range(1, 21)}        # flat → excess == insurer ret
    panel = pd.DataFrame({"ticker": ["PGR"], "as_of": ["2026-06-01"],
                          **{c: [np.nan] for c in features.FEATURE_COLUMNS}})
    out = alpha.add_labels(panel, {"PGR": closes}, bench, horizon=5)
    # close[06-06]=106 vs close[06-01]=101 → 106/101 - 1; benchmark flat.
    assert out.iloc[0]["fwd_excess"] == 106.0 / 101.0 - 1.0


def test_add_labels_nan_when_window_incomplete():
    closes = {f"2026-06-{d:02d}": 100.0 + d for d in range(1, 11)}
    panel = pd.DataFrame({"ticker": ["PGR"], "as_of": ["2026-06-09"],   # only 1 day forward
                          **{c: [np.nan] for c in features.FEATURE_COLUMNS}})
    out = alpha.add_labels(panel, {"PGR": closes}, {}, horizon=5)
    assert np.isnan(out.iloc[0]["fwd_excess"])


# ── purged walk-forward ─────────────────────────────────────────────────────


def test_walk_forward_train_is_purged_before_each_test_block():
    days = [(date(2026, 1, 1) + timedelta(days=i)).isoformat() for i in range(120)]
    folds = list(alpha.walk_forward_folds(days, n_splits=3, embargo_days=20))
    assert len(folds) >= 2
    for train_d, test_d in folds:
        test_start = min(test_d)
        # every training date is < test_start minus the embargo, and < all test dates
        assert max(train_d) < test_start
        gap = (date.fromisoformat(test_start) - date.fromisoformat(max(train_d))).days
        assert gap >= 20                       # embargo honored
        assert not (train_d & test_d)          # no overlap


# ── train → predict integration (synthetic, via build_panel monkeypatch) ────


def _seed_prices(tickers, bench, n_days=130):
    base = date(2026, 1, 1)
    rng = np.random.RandomState(0)
    panel_rows = []
    for t in tickers:
        closes = []
        price = 100.0
        for i in range(n_days):
            d = (base + timedelta(days=i)).isoformat()
            price *= 1.0 + rng.normal(0, 0.01)
            closes.append({"ticker": t, "date": d, "close": price,
                           "kind": "insurer", "source": "x", "fetched_at": "now"})
        db.upsert_prices(closes)
        # synthetic feature row per day; one feature mildly tracks next-day drift.
        for i in range(n_days):
            d = (base + timedelta(days=i)).isoformat()
            row = {"ticker": t, "as_of": d, **{c: 0.0 for c in features.FEATURE_COLUMNS}}
            row["sig_score_sum"] = float(rng.normal(0, 1))
            row["vol_20d"] = 0.01
            panel_rows.append(row)
    bench_rows = [{"ticker": bench, "date": (base + timedelta(days=i)).isoformat(),
                   "close": 200.0, "kind": "benchmark", "source": "x", "fetched_at": "now"}
                  for i in range(n_days)]
    db.upsert_prices(bench_rows)
    return pd.DataFrame(panel_rows)


def test_train_and_predict_end_to_end(fresh_db, monkeypatch):
    panel = _seed_prices(["PGR", "ALL"], "IAK")
    monkeypatch.setattr(alpha.features, "build_panel", lambda *a, **k: panel.copy())

    summary = alpha.train(horizon=10, n_splits=3)
    assert summary["model_id"] is not None
    assert summary["n"] >= alpha.MIN_LABELED
    # the scorecard is populated (values may be noisy — we don't assert an edge)
    assert "ic" in summary and "baseline_ic" in summary

    meta = db.latest_return_model("excess_return")
    assert meta is not None and meta["algo"] in {"histgb", "lightgbm"}

    pred = alpha.predict(model_id=meta["id"])
    assert pred["forecasts"] == 2            # one per insurer (latest as-of row)
    rows = db.latest_return_forecasts(horizon_days=meta["horizon_days"])
    assert {r["ticker"] for r in rows} == {"PGR", "ALL"}
    assert all(r["pred_excess"] is not None for r in rows)


def test_train_gates_on_too_little_data(fresh_db, monkeypatch):
    small = pd.DataFrame([{"ticker": "PGR", "as_of": "2026-01-01",
                           **{c: 0.0 for c in features.FEATURE_COLUMNS}}])
    monkeypatch.setattr(alpha.features, "build_panel", lambda *a, **k: small.copy())
    monkeypatch.setattr(db, "price_closes", lambda t: {})
    r = alpha.train(horizon=10)
    assert r["model_id"] is None and "labeled rows" in r["note"]


def test_has_edge_requires_positive_ic_beating_baseline():
    assert alpha.has_edge(0.08, 0.02) is True        # positive and beats baseline
    assert alpha.has_edge(0.05, None) is True         # positive, no baseline
    assert alpha.has_edge(-0.04, -0.07) is False      # less-negative is NOT an edge
    assert alpha.has_edge(0.01, 0.05) is False        # positive but worse than baseline
    assert alpha.has_edge(0.0, None) is False         # zero is not an edge
    assert alpha.has_edge(None, None) is False
