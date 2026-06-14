"""Option 5 — reserving: chain-ladder math, signal, run job, boost.

Plus EKG Lead 6: PDF-table → loss-triangle structuring (parse.triangles), which
closes the only missing link in the otherwise-wired reserving chain.
"""
from __future__ import annotations

import numpy as np
import pytest

from digest import db, reserving
from digest.parse.pdf_tables import Table
from digest.parse.triangles import looks_like_triangle, parse_triangle


# ── Lead 6: triangle structurer (parse.triangles) ─────────────────────────


def _standard_triangle() -> Table:
    """AY-rows × dev-cols, the known {IBNR 94.5} triangle in PDF-table shape."""
    return Table(
        page=1,
        header=["Accident Year", "12", "24", "36"],
        rows=[
            ["2023", "100", "150", "165"],
            ["2024", "110", "165", ""],
            ["2025", "120", "", ""],
        ],
    )


def test_parse_triangle_standard_orientation():
    cells = parse_triangle(_standard_triangle(), insurer="PGR", lob="auto",
                           metric="incurred", as_of="2026-03-31")
    got = {(c["accident_year"], c["dev_period"]): c["cumulative_value"] for c in cells}
    assert got == {
        (2023, 12): 100.0, (2023, 24): 150.0, (2023, 36): 165.0,
        (2024, 12): 110.0, (2024, 24): 165.0,
        (2025, 12): 120.0,
    }
    # every cell carries the snapshot key + metadata
    assert all(c["insurer"] == "PGR" and c["metric"] == "incurred"
               and c["as_of"] == "2026-03-31" for c in cells)


def test_parse_triangle_transposed_orientation():
    """Dev-rows × AY-cols must yield the same cells as the standard layout."""
    transposed = Table(
        page=1,
        header=["Months", "2023", "2024", "2025"],
        rows=[
            ["12", "100", "110", "120"],
            ["24", "150", "165", ""],
            ["36", "165", "", ""],
        ],
    )
    cells = parse_triangle(transposed, insurer="PGR", lob="auto",
                           metric="incurred", as_of="2026-03-31")
    got = {(c["accident_year"], c["dev_period"]): c["cumulative_value"] for c in cells}
    assert got == {
        (2023, 12): 100.0, (2023, 24): 150.0, (2023, 36): 165.0,
        (2024, 12): 110.0, (2024, 24): 165.0,
        (2025, 12): 120.0,
    }


def test_parse_triangle_accounting_numbers_and_subtotals():
    tbl = Table(
        page=1,
        header=["AY", "12", "24", "36"],
        rows=[
            ["2023", "$1,200", "(50)", "1,300"],   # $, parens-negative, comma
            ["2024", "1,100", "1,250", "n/a"],      # n/a → unobserved
            ["Total", "2,300", "1,200", "1,300"],   # subtotal row → skipped
        ],
    )
    cells = parse_triangle(tbl, insurer="ALL", lob="home",
                           metric="paid", as_of="2026-03-31")
    got = {(c["accident_year"], c["dev_period"]): c["cumulative_value"] for c in cells}
    assert got == {
        (2023, 12): 1200.0, (2023, 24): -50.0, (2023, 36): 1300.0,
        (2024, 12): 1100.0, (2024, 24): 1250.0,
    }


def test_parse_triangle_chain_ladder_roundtrip():
    """Cells parsed from a PDF table develop to the hand-computed IBNR."""
    cells = parse_triangle(_standard_triangle(), insurer="PGR", lob="auto",
                           metric="incurred", as_of="2026-03-31")
    _, mat = reserving.build_matrix(cells)
    cl = reserving.chain_ladder(mat)
    assert cl["ibnr"] == pytest.approx(94.5)
    assert cl["ultimate_total"] == pytest.approx(544.5)


def test_looks_like_triangle_rejects_non_triangle():
    not_a_triangle = Table(
        page=1,
        header=["Segment", "Net premiums", "Loss ratio"],
        rows=[["Personal auto", "$1,234", "92.1%"], ["Homeowners", "$567", "78.4%"]],
    )
    assert looks_like_triangle(not_a_triangle) is False
    assert parse_triangle(not_a_triangle, insurer="X", lob="y",
                          metric="paid", as_of="2026-03-31") == []
    assert looks_like_triangle(_standard_triangle()) is True


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
    # One-year development on prior AYs: AY23 165−150=15, AY24 165−110=55, AY25 one
    # cell → excluded ⇒ 70 (the newest AY contributes nothing, by construction).
    assert cl["cy_development"] == pytest.approx(70.0)


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


def test_reserve_signal_adverse_from_within_filing_development():
    """deterioration_pct = cy_development / ultimate (signed rate), computed from the
    single filing — independent of any prior snapshot."""
    cl = {"ultimate_total": 544.5, "latest_total": 450.0, "ibnr": 94.5, "cy_development": 70.0}
    sig = reserving.reserve_signal("PGR", "auto", "incurred", "2026-05-01", cl)
    assert sig["direction"] == "adverse"
    assert sig["cy_development"] == 70.0
    assert sig["deterioration_pct"] == pytest.approx(70.0 / 544.5, abs=1e-3)


def test_reserve_signal_favorable_and_missing_development():
    favorable = reserving.reserve_signal(
        "PGR", "auto", "incurred", "2026-05-01",
        {"ultimate_total": 544.5, "latest_total": 450.0, "ibnr": 94.5, "cy_development": -30.0})
    assert favorable["direction"] == "favorable" and favorable["deterioration_pct"] < 0

    # No development reading (e.g. a single-diagonal triangle) → None, regardless of
    # prior_ibnr; the signal no longer depends on a prior snapshot.
    none_sig = reserving.reserve_signal(
        "PGR", "auto", "incurred", "2026-05-01",
        {"ultimate_total": 544.5, "latest_total": 450.0, "ibnr": 94.5}, prior_ibnr=80.0)
    assert none_sig["deterioration_pct"] is None and none_sig["direction"] is None


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
    # One snapshot now suffices: the within-filing diagonal gives development
    # (AY23 +15, AY24 +55 ⇒ cy_development 70 on ultimate 544.5 → adverse).
    assert rows[0]["direction"] == "adverse"
    assert rows[0]["cy_development"] == pytest.approx(70.0)
    assert rows[0]["deterioration_pct"] == pytest.approx(70.0 / 544.5, abs=1e-3)


# ── triangle_keys: deterioration when both snapshots are loaded in one session ─

def _scaled_triangle(scale: float) -> Table:
    def s(v: float) -> str:
        return str(round(v * scale, 2))
    return Table(page=1, header=["Accident Year", "12", "24", "36"],
                 rows=[["2023", s(100), s(150), s(165)],
                       ["2024", s(110), s(165), ""],
                       ["2025", s(120), "", ""]])


def test_run_reserving_computes_every_snapshot(fresh_db):
    """Regression: run_reserving computes EACH stored snapshot, not just MAX(as_of)
    (triangle_keys returns all). Two annual snapshots loaded in one session → 2
    estimates. (The deterioration signal itself now reads a single filing.)"""
    db.upsert_triangle_cells(parse_triangle(
        _scaled_triangle(1.0), insurer="PGR", lob="auto", metric="incurred", as_of="2024-12-31"))
    db.upsert_triangle_cells(parse_triangle(
        _scaled_triangle(1.3), insurer="PGR", lob="auto", metric="incurred", as_of="2025-12-31"))

    counts = reserving.run_reserving()
    assert counts["computed"] == 2              # prior AND latest snapshot


def test_reserving_severity_map_exposure_weighted_within_filing(fresh_db):
    """The reserve boost reads one-year INCURRED development off a single filing's
    diagonal, normalized by chain-ladder ultimate and scaled by DEVELOPMENT_BOOST_K.
    No second snapshot needed."""
    db.upsert_triangle_cells(parse_triangle(
        _standard_triangle(), insurer="PGR", lob="auto", metric="incurred",
        as_of="2025-12-31"))                    # cy_development 70, ultimate 544.5 (> floor)
    reserving.run_reserving()

    expected = reserving.DEVELOPMENT_BOOST_K * (70.0 / 544.5)   # boost-scale severity
    assert db.reserving_severity_map().get("PGR", 0.0) == pytest.approx(expected, abs=1e-3)


def test_reserving_severity_map_floor_excludes_tiny_lines(fresh_db):
    """A LOB below the materiality floor (Σ ultimate) can't enter the insurer
    aggregate, so a small line on a near-zero base never swings the reserve signal."""
    db.upsert_triangle_cells(parse_triangle(
        _scaled_triangle(0.1), insurer="XYZ", lob="tiny", metric="incurred",
        as_of="2025-12-31"))                    # ultimate ≈ 54 < RESERVE_ULTIMATE_FLOOR (250)
    reserving.run_reserving()
    assert db.reserving_severity_map().get("XYZ") is None


def test_reserving_severity_map_respects_tunables(fresh_db):
    """k and floor are user-tunable (Scoring Weights.md `reserving` section): k scales
    the boost-scale severity; a high floor excludes an otherwise-material line."""
    db.upsert_triangle_cells(parse_triangle(
        _standard_triangle(), insurer="PGR", lob="auto", metric="incurred",
        as_of="2025-12-31"))                    # cy_development 70, ultimate 544.5
    reserving.run_reserving()

    base = reserving.DEVELOPMENT_BOOST_K * (70.0 / 544.5)
    assert db.reserving_severity_map(k=2 * reserving.DEVELOPMENT_BOOST_K).get("PGR") == \
        pytest.approx(2 * base, abs=1e-3)       # doubling k doubles the severity
    assert db.reserving_severity_map(floor=10_000.0).get("PGR") is None   # floor excludes it
