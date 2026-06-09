"""Alpha engine Phase 2 — as-of feature panel: no lookahead, correct aggregation."""
from __future__ import annotations

import numpy as np
import pandas as pd

from digest import features


def _signals(rows):
    return pd.DataFrame(rows, columns=[
        "ticker", "event_date", "score", "learned_score", "materiality", "topic"])


def _empty_asof(*cols):
    return pd.DataFrame(columns=list(cols))


def test_signal_window_is_strictly_trailing():
    # Items on 06-04 (in window), 06-01 (just inside 5d), 05-30 (outside),
    # and 06-06 (FUTURE — must never count for an as_of of 06-05).
    sig = _signals([
        ("PGR", "2026-06-04", 2.0, 0.7, 0.8, "reserving"),
        ("PGR", "2026-06-01", 1.0, None, None, "social_inflation"),
        ("PGR", "2026-05-30", 9.0, 0.9, 0.9, "reserving"),   # outside 5d window
        ("PGR", "2026-06-06", 9.0, 0.9, 0.9, "reserving"),   # the future
    ])
    panel = features.assemble_panel(
        ["PGR"], ["2026-06-05"], sig, {"PGR": {}},
        _empty_asof("insurer", "as_of", "deterioration_pct"),
        _empty_asof("insurer", "as_of", "adverse_language_score"),
        _empty_asof("as_of", "market_mult", "cat_mult"),
        signal_window=5)
    row = panel.iloc[0]
    assert row["sig_count"] == 2                  # only 06-04 and 06-01
    assert row["sig_score_sum"] == 3.0            # 2.0 + 1.0, the future excluded
    assert row["sig_score_max"] == 2.0
    assert row["n_reserving"] == 1                # 05-30 and the future excluded
    assert row["n_social_inflation"] == 1
    assert row["sig_learned_mean"] == 0.7         # the None is dropped, not 0


def test_price_features_use_only_past_closes():
    # 70 ascending daily closes; the last one in-range for as_of is index 59.
    closes = {f"2026-{3 + (i // 28):02d}-{(i % 28) + 1:02d}": 100.0 + i for i in range(70)}
    dates = sorted(closes)
    as_of = dates[59]
    feats = features._price_features(closes, as_of)
    # ret_5d uses close[59] vs close[54]; momentum vs close[-61] = index 59-? present.
    assert feats["ret_5d"] is not None and not np.isnan(feats["ret_5d"])
    # A future close (index 65) must not change the as_of=index59 features.
    feats_future_hidden = features._price_features(
        {d: c for d, c in closes.items() if d <= as_of}, as_of)
    assert feats["ret_5d"] == feats_future_hidden["ret_5d"]
    assert feats["vol_20d"] == feats_future_hidden["vol_20d"]


def test_price_features_nan_when_insufficient_history():
    feats = features._price_features({"2026-06-01": 100.0}, "2026-06-05")
    assert np.isnan(feats["ret_5d"]) and np.isnan(feats["vol_20d"])


def test_asof_join_picks_latest_at_or_before():
    res = pd.DataFrame([
        {"insurer": "PGR", "as_of": "2026-05-01", "deterioration_pct": 0.05},
        {"insurer": "PGR", "as_of": "2026-06-01", "deterioration_pct": 0.12},
        {"insurer": "PGR", "as_of": "2026-07-01", "deterioration_pct": 0.20},  # future
    ])
    panel = features.assemble_panel(
        ["PGR"], ["2026-06-15"], _signals([]), {"PGR": {}},
        res,
        _empty_asof("insurer", "as_of", "adverse_language_score"),
        _empty_asof("as_of", "market_mult", "cat_mult"))
    # 06-01 reading applies at 06-15; the 07-01 future reading does not.
    assert panel.iloc[0]["reserve_deterioration"] == 0.12


def test_full_feature_contract_present_even_with_empty_inputs():
    panel = features.assemble_panel(
        ["PGR"], ["2026-06-15"], _signals([]), {},
        _empty_asof("insurer", "as_of", "deterioration_pct"),
        _empty_asof("insurer", "as_of", "adverse_language_score"),
        _empty_asof("as_of", "market_mult", "cat_mult"))
    assert list(panel.columns) == ["ticker", "as_of", *features.FEATURE_COLUMNS]
    assert panel.iloc[0]["regime_market_mult"] == 1.0   # default when no regime data
    assert panel.iloc[0]["sig_count"] == 0.0
