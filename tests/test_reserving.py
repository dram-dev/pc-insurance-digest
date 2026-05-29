"""Option 5 — reserving: chain-ladder math, signal, run job, boost."""
from __future__ import annotations

import numpy as np
import pytest

from digest import db, reserving


# ── chain-ladder math (hand-computed triangle) ───────────────────────────
# AY0: 100,150,165  | AY1: 110,165,–  | AY2: 120,–,–
#   f(0→1) = (150+165)/(100+110) = 1.5 ; f(1→2) = 165/150 = 1.1
#   cdf = [1.65, 1.1, 1.0]
#   ult = 165 + 165*1.1 + 120*1.65 = 544.5 ; latest = 450 ; IBNR = 94.5
NAN = float("nan")


def test_chain_ladder_known_triangle():
    mat = np.array([[100, 150, 165], [110, 165, NAN], [120, NAN, NAN]], dtype=float)
    cl = reserving.chain_ladder(mat)
    assert cl["dev_factors"] == pytest.approx([1.5, 1.1], rel=1e-6)
    assert cl["cdf"] == pytest.approx([1.65, 1.1, 1.0], rel=1e-6)
    assert cl["latest_total"] == pytest.approx(450.0)
    assert cl["ultimate_total"] == pytest.approx(544.5)
    assert cl["ibnr"] == pytest.approx(94.5)


def test_chain_ladder_too_sparse():
    assert reserving.chain_ladder(np.array([[100.0]])) is None


def test_build_matrix():
    rows = [
        {"accident_year": 2024, "dev_period": 0, "cumulative_value": 100.0},
        {"accident_year": 2024, "dev_period": 1, "cumulative_value": 150.0},
        {"accident_year": 2025, "dev_period": 0, "cumulative_value": 110.0},
    ]
    ays, mat = reserving.build_matrix(rows)
    assert ays == [2024, 2025]
    assert mat[0, 1] == 150.0 and np.isnan(mat[1, 1])


# ── deterioration signal ──────────────────────────────────────────────────


def test_reserve_signal_adverse():
    cl = {"ultimate_total": 544.5, "latest_total": 450.0, "ibnr": 94.5}
    sig = reserving.reserve_signal("PGR", "auto", "incurred", "2026-05-01", cl, prior_ibnr=80.0)
    assert sig["direction"] == "adverse"
    assert sig["deterioration_pct"] == pytest.approx((94.5 - 80) / 80, abs=1e-3)


def test_reserve_signal_no_prior():
    cl = {"ultimate_total": 544.5, "latest_total": 450.0, "ibnr": 94.5}
    sig = reserving.reserve_signal("PGR", "auto", "incurred", "2026-05-01", cl, prior_ibnr=None)
    assert sig["deterioration_pct"] is None and sig["direction"] is None


# ── boost (pure, not yet wired into the formula) ──────────────────────────


def test_reserve_deterioration_boost():
    sev = {"PGR": 0.2, "ALL": 0.05}
    assert reserving.reserve_deterioration_boost("Progressive adverse reserve charge", sev) == pytest.approx(1.2)
    assert reserving.reserve_deterioration_boost("Allstate develops", sev) == pytest.approx(1.05)
    assert reserving.reserve_deterioration_boost("no insurer here", sev) == 1.0
    assert reserving.reserve_deterioration_boost("Progressive", {}) == 1.0


def test_reserve_deterioration_boost_capped():
    assert reserving.reserve_deterioration_boost("Chubb", {"CB": 0.9}) == reserving.RESERVE_BOOST_CAP


# ── run job (seeded triangles) ─────────────────────────────────────────────


def test_run_reserving_persists_signal(fresh_db):
    cells = []
    tri = {(0, 0): 100, (0, 1): 150, (0, 2): 165, (1, 0): 110, (1, 1): 165, (2, 0): 120}
    for (ay, dev), val in tri.items():
        cells.append({"insurer": "PGR", "lob": "auto", "metric": "incurred",
                      "accident_year": 2023 + ay, "dev_period": dev,
                      "cumulative_value": float(val), "as_of": "2026-05-01"})
    db.upsert_triangle_cells(cells)

    counts = reserving.run_reserving()
    assert counts == {"triangles": 1, "computed": 1}
    rows = db.latest_reserving_signals()
    assert len(rows) == 1
    assert rows[0]["insurer"] == "PGR"
    assert rows[0]["ibnr"] == pytest.approx(94.5)
    assert rows[0]["direction"] is None              # no prior estimate yet
