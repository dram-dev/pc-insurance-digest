"""Isotonic materiality calibration — PAVA math, relativity clamps, gates, wiring."""
from __future__ import annotations

import numpy as np
import pytest

from digest import calibration, db, signals
from digest.calibration import IsotonicCalibrator, pava


# ── PAVA math ──────────────────────────────────────────────────────────────


def test_pava_monotone_on_noisy_data():
    rng = np.random.RandomState(0)
    x = rng.uniform(0.5, 1.5, 200)
    y = (rng.uniform(size=200) < np.clip(x - 0.5, 0, 1)).astype(float)  # P up in x
    _, by = pava(x, y)
    assert by == sorted(by)                   # non-decreasing by construction


def test_pava_pools_violators():
    # y dips in the middle — PAVA must pool the dip into one block.
    bx, by = pava([1.0, 2.0, 3.0, 4.0], [0.0, 0.8, 0.2, 1.0])
    assert by == sorted(by)
    assert min(by) >= 0.0 and max(by) <= 1.0
    # The 0.8/0.2 violation pools to their mean (equal weights → 0.5).
    assert 0.5 in [round(b, 6) for b in by]


def test_pava_pre_pools_ties():
    bx, _ = pava([1.0, 1.0, 2.0], [0.0, 1.0, 1.0])
    assert len(bx) == len(set(bx))            # one block per distinct x at most


def test_predict_steps_and_clamps_at_ends():
    cal = IsotonicCalibrator([0.7, 1.0, 1.3], [0.2, 0.4, 0.8], base_rate=0.4)
    assert cal.predict(0.1) == 0.2            # below the curve → first block (flat)
    assert cal.predict(0.85) == 0.2           # inside [0.7, 1.0)
    assert cal.predict(1.0) == 0.4            # right-continuous at the breakpoint
    assert cal.predict(9.9) == 0.8            # above the curve → last block (flat)


# ── judgment relativity ────────────────────────────────────────────────────


def test_judgment_is_relativity_anchored_at_base_rate():
    cal = IsotonicCalibrator([0.7, 1.0, 1.3], [0.2, 0.4, 0.8], base_rate=0.4)
    assert cal.judgment(1.0) == pytest.approx(1.0)     # average item → exactly neutral
    assert cal.judgment(1.3) == pytest.approx(1.5)     # 0.8/0.4 = 2.0 → clamped 1.5
    assert cal.judgment(0.7) == pytest.approx(0.5)     # 0.2/0.4 = 0.5 → at the floor
    assert cal.judgment(None) == 1.0                   # garbage in → neutral out
    assert cal.judgment("n/a") == 1.0


def test_judgment_neutral_on_degenerate_base_rate():
    cal = IsotonicCalibrator([1.0], [0.0], base_rate=0.0)
    assert cal.judgment(1.2) == 1.0


def test_calibrator_json_roundtrip():
    cal = IsotonicCalibrator([0.7, 1.3], [0.25, 0.75], base_rate=0.5)
    cal2 = IsotonicCalibrator.from_json(cal.to_json())
    assert cal2.block_x == cal.block_x and cal2.block_y == cal.block_y
    assert cal2.judgment(1.3) == cal.judgment(1.3)


# ── train gates + persistence ──────────────────────────────────────────────


def _seed_labeled_materiality(make_item, n, pos_frac=0.5):
    """n labeled items whose corroboration is monotone in materiality."""
    for i in range(n):
        sid = f"c{i}"
        m = 0.5 + (i / max(n - 1, 1))                      # 0.5 .. 1.5
        db.upsert_items([make_item(source="rss", source_id=sid, title=f"t{i}")])
        with db.get_conn() as conn:
            iid = conn.execute("SELECT id FROM items WHERE source_id=?", (sid,)).fetchone()["id"]
            conn.execute(
                "UPDATE items SET triage_decision='keep', materiality_score=? WHERE id=?",
                (m, iid))
        db.upsert_signal_scores([{
            "item_id": iid, "computed_at": f"2026-01-01T00:{i // 60:02d}:{i % 60:02d}",
            "score": 1.0, "source_mult": 1.0, "regime_mult": 1.0, "topic_relevance": 1.0,
            "recency": 1.0, "llm_judgment": 1.0, "topic_boost": 1.0, "burden_boost": 1.0,
            "insurer_boost": 1.0, "inflation_boost": 1.0, "regulatory_boost": 1.0,
            "tplf_boost": 1.0, "tier": "low",
        }])
        db.upsert_backtest_outcome(iid, 30, {
            "corroborated": i >= n * (1 - pos_frac),       # top materiality corroborates
            "signals": [],
        })


def test_train_gated_below_min_labeled(fresh_db, make_item):
    _seed_labeled_materiality(make_item, n=40)
    out = calibration.train_materiality_calibrator(horizon_days=30)
    assert out["calibrator_id"] is None and "need ≥100" in out["note"]
    assert calibration.latest_materiality_calibrator() is None


def test_train_fits_persists_and_loads(fresh_db, make_item):
    _seed_labeled_materiality(make_item, n=120, pos_frac=0.4)
    out = calibration.train_materiality_calibrator(horizon_days=30)
    assert out["calibrator_id"] is not None and out["n_samples"] == 120
    cal = calibration.latest_materiality_calibrator()
    assert cal is not None
    # Monotone: higher materiality can never map to a lower judgment.
    assert cal.judgment(1.5) >= cal.judgment(1.0) >= cal.judgment(0.5)
    # Separation: the seeded label IS monotone in materiality, so the ends differ.
    assert cal.judgment(1.5) > cal.judgment(0.5)


def test_train_gated_on_single_class(fresh_db, make_item):
    _seed_labeled_materiality(make_item, n=120, pos_frac=0.0)   # nothing corroborates
    out = calibration.train_materiality_calibrator(horizon_days=30)
    assert out["calibrator_id"] is None


# ── wiring into score_item ─────────────────────────────────────────────────


class _Row(dict):
    def keys(self):
        return super().keys()


def _item_row(materiality):
    return _Row(
        id=1, source="rss", topic="cyber",
        published_at=None, ingested_at="2026-06-09T00:00:00+00:00",
        materiality_score=materiality,
    )


def test_score_item_uses_calibrator_when_given():
    regime = type("_Regime", (), {"multiplier": 1.0})()
    cal = IsotonicCalibrator([0.7, 1.3], [0.2, 0.8], base_rate=0.4)
    raw = signals.score_item(_item_row(1.3), regime)
    calibrated = signals.score_item(_item_row(1.3), regime, calibrator=cal)
    assert raw.llm_judgment == pytest.approx(1.3)          # raw clamp path
    assert calibrated.llm_judgment == pytest.approx(1.5)   # 0.8/0.4 → clamped 1.5


def test_score_item_raw_clamp_without_calibrator():
    regime = type("_Regime", (), {"multiplier": 1.0})()
    s = signals.score_item(_item_row(9.0), regime)         # raw clamps to 1.5
    assert s.llm_judgment == pytest.approx(1.5)
