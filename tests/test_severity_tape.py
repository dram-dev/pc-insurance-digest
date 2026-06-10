"""Severity Tape (EKG Lead 3) — FRED blend + inflation-boost magnitude scaling.

Network-free: run_severity_tape takes an injectable fetch returning synthetic
FRED observations; the boost scaling is driven directly.
"""
from __future__ import annotations

from digest import db, severity_tape, signals


def _obs(values: list[float]) -> list[dict]:
    """Synthetic FRED observation list (≤12 months in 2025), oldest first."""
    return [{"date": f"2025-{m:02d}-01", "value": str(v)}
            for m, v in enumerate(values, start=1)]


def _monthly(values: list[float], year: int = 2022, month: int = 1) -> list[dict]:
    """Synthetic FRED observations spanning multiple years (rolls over December)."""
    out = []
    for v in values:
        out.append({"date": f"{year:04d}-{month:02d}-01", "value": str(v)})
        month += 1
        if month > 12:
            year, month = year + 1, 1
    return out


def test_inflation_boost_scales_when_tape_is_hot():
    blob = "auto parts costs surged on repair cost inflation"
    # No tape reading → flat boost value.
    assert signals._inflation_keyword_boost(blob, 1.2, None) == 1.2
    # Mild severity → unchanged.
    assert signals._inflation_keyword_boost(blob, 1.2, 1.0) == 1.2
    # Hot severity regime → uplifted, capped.
    assert signals._inflation_keyword_boost(blob, 1.2, 2.5) == 1.3
    assert signals._inflation_keyword_boost(blob, 1.35, 2.5) == 1.4   # cap
    # No keyword hit → always 1.0 regardless of severity.
    assert signals._inflation_keyword_boost("a quiet cyber update", 1.2, 3.0) == 1.0


def test_run_severity_tape_blends_components(fresh_db):
    # Stable baseline then a final spike → an elevated m/m z for every series.
    spike = _obs([100, 101, 100, 102, 101, 100, 101, 102, 100, 101, 100, 130])

    written = severity_tape.run_severity_tape(_fetch=lambda sid: spike)
    assert written["components"] > 0
    # A full monthly tape now: one level row per (series, month) + a blended row
    # per month — not a single point per series.
    assert written["written"] == written["components"] * 12 + 12

    with db.get_conn() as conn:
        dates = [r["observation_date"] for r in conn.execute(
            "SELECT DISTINCT observation_date FROM severity_index "
            "WHERE index_name='blended_severity' ORDER BY observation_date")]
    assert dates == [f"2025-{m:02d}-01" for m in range(1, 13)]

    blended = db.latest_severity_index("blended_severity")
    assert blended is not None
    assert blended["category"] == "blended"
    assert blended["observation_date"] == "2025-12-01"
    assert blended["value"] > 100                           # a rebased LEVEL, not a m/m %
    assert severity_tape.severity_regime() == blended["zscore_12m"]
    assert severity_tape.severity_regime() > 1.5            # spike → hot


def test_tape_stores_a_trendable_level_series(fresh_db):
    # A steadily compounding series must land as a positive, monotone level tape
    # so the severity-trend-decomposition skill can fit ln(value).
    rising = _monthly([100 * 1.005 ** i for i in range(24)])

    severity_tape.run_severity_tape(_fetch=lambda sid: rising)

    with db.get_conn() as conn:
        vals = [r["value"] for r in conn.execute(
            "SELECT value FROM severity_index WHERE index_name='blended_severity' "
            "ORDER BY observation_date")]
    assert len(vals) >= 12
    assert all(v > 0 for v in vals)                          # ln() defined
    assert all(b > a for a, b in zip(vals, vals[1:]))        # monotone → trend-fit-able


def test_blended_rows_respect_loss_cost_weights():
    # Component A doubles, component B is flat. At 0.9/0.1 weights the blend
    # must sit near A; equal weights (None) reproduce the pre-PR4 midpoint.
    levels_a = {"2025-01-01": 100.0, "2025-02-01": 200.0}
    levels_b = {"2025-01-01": 50.0, "2025-02-01": 50.0}      # different base → rebased
    weighted = severity_tape._blended_rows(
        [levels_a, levels_b], [{}, {}], "now", [0.9, 0.1])
    assert weighted[-1]["value"] == 0.9 * 200.0 + 0.1 * 100.0   # rebased to 100 each
    equal = severity_tape._blended_rows([levels_a, levels_b], [{}, {}], "now")
    assert equal[-1]["value"] == 150.0


def test_blended_z_is_weight_renormalized_over_present_components():
    levels = [{"2025-01-01": 100.0, "2025-02-01": 101.0}] * 2
    zs = [{"2025-02-01": 2.0}, {}]                            # only A has a z
    rows = severity_tape._blended_rows(levels, zs, "now", [0.3, 0.7])
    assert rows[-1]["zscore_12m"] == 2.0                      # renormalized to A alone


def test_rolling_z_robust_resists_outlier_inflation():
    # One 10% outlier print inflates the classic σ and masks the next hot
    # print; the median/MAD z does not get fooled.
    pcts = [0.0, 0.2, 0.0, 0.2, 0.0, 10.0, 0.2, 0.0, 0.2, 0.0, 0.2, 1.5]
    mom = [(f"2025-{m:02d}-01", p) for m, p in enumerate(pcts, start=1)]
    classic = severity_tape._rolling_z(mom)["2025-12-01"]
    robust = severity_tape._rolling_z(mom, robust=True)["2025-12-01"]
    assert classic < 1.0          # outlier-inflated σ hides the 1.5% print
    assert robust > 3.0           # MAD scale flags it


def test_severity_regime_none_without_data(fresh_db):
    assert severity_tape.severity_regime() is None


def test_run_severity_tape_handles_unusable_series(fresh_db):
    # Too few points for any z → nothing written, no crash.
    assert severity_tape.run_severity_tape(_fetch=lambda sid: _obs([100, 101])) == {
        "components": 0, "written": 0,
    }
