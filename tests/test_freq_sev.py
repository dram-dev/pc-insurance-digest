"""Frequency × severity × pure-premium derivation from the XBRL facts."""
from __future__ import annotations

import pytest

from digest import db, freq_sev as fs

_DIMS = dict(period_type="duration", accident_year=None, segment=None,
             product=None, subsegment=None, geography=None,
             investment_type=None, instrument=None, fv_level=None, is_count=0)


def _xf(fact_key, dataset, field, value, **kw):
    row = {"fact_key": fact_key, "insurer": "PGR", "dataset": dataset, "concept": "C",
           "field": field, "period_end": "2025-12-31", "value": value,
           "as_of": "2025-12-31", **_DIMS}
    row.update(kw)
    return row


def _seed_basic():
    """One product line, two mature AYs + the immature latest AY, segment EP."""
    seg, prod = "personal_lines_segment", "auto_liability"
    facts = []
    # incurred ($M) and counts: severity 10,000 in 2023, 11,000 in 2024.
    for i, (ay, claims, inc) in enumerate([(2023, 100_000, 1000.0),
                                           (2024, 100_000, 1100.0),
                                           (2025, 40_000, 500.0)]):
        facts.append(_xf(f"c{i}", "claim_counts", "reported_claims", claims,
                         accident_year=ay, segment=seg, product=prod, is_count=1))
        facts.append(_xf(f"i{i}", "triangle", "incurred", inc,
                         accident_year=ay, segment=seg, product=prod))
    # Segment earned premium per calendar year ($M).
    for j, (year, ep) in enumerate([(2023, 2000.0), (2024, 2200.0), (2025, 2500.0)]):
        facts.append(_xf(f"p{j}", "premiums", "premiums_earned_net", ep,
                         period_end=f"{year}-12-31", segment=seg))
    db.upsert_xbrl_facts(facts)


def test_product_severity_incurred_over_counts(fresh_db):
    _seed_basic()
    detail = fs.derive_insurer("pgr")
    prod = {r["accident_year"]: r for r in detail if r["grain"] == "product"}
    assert prod[2023]["severity_usd"] == 10_000.0       # 1000 $M / 100k claims
    assert prod[2024]["severity_usd"] == 11_000.0
    assert prod[2023]["earned_premium_musd"] is None    # EP is segment grain only


def test_segment_frequency_and_pure_premium(fresh_db):
    _seed_basic()
    detail = fs.derive_insurer("PGR")
    seg = {r["accident_year"]: r for r in detail if r["grain"] == "segment"}
    assert seg[2023]["frequency_per_musd"] == 50.0      # 100k claims / 2000 $M EP
    assert seg[2023]["pure_premium_ratio"] == 0.5       # 1000 / 2000
    # freq × sev reconciles to pure premium exactly (matched cells only).
    r = seg[2024]
    assert r["frequency_per_musd"] * r["severity_usd"] / 1e6 == pytest.approx(
        r["pure_premium_ratio"], rel=1e-3)


def test_latest_evaluation_wins(fresh_db):
    """Two evaluations of the same AY → the later period_end is the estimate."""
    seg, prod = "personal_lines_segment", "auto_liability"
    db.upsert_xbrl_facts([
        _xf("i_old", "triangle", "incurred", 900.0, accident_year=2023,
            segment=seg, product=prod, period_end="2023-12-31"),
        _xf("i_new", "triangle", "incurred", 1000.0, accident_year=2023,
            segment=seg, product=prod, period_end="2025-12-31"),
        _xf("c1", "claim_counts", "reported_claims", 100_000, accident_year=2023,
            segment=seg, product=prod, is_count=1),
    ])
    detail = fs.derive_insurer("PGR")
    prod_row = next(r for r in detail if r["grain"] == "product")
    assert prod_row["severity_usd"] == 10_000.0          # 1000 $M, not 900


def test_subsegments_aggregate_to_segment(fresh_db):
    """Agency + direct channel cells sum into one segment row per AY."""
    seg, prod = "personal_lines_segment", "auto_liability"
    db.upsert_xbrl_facts([
        _xf("ca", "claim_counts", "reported_claims", 60_000, accident_year=2023,
            segment=seg, product=prod, subsegment="agency_channel", is_count=1),
        _xf("cd", "claim_counts", "reported_claims", 40_000, accident_year=2023,
            segment=seg, product=prod, subsegment="direct_channel", is_count=1),
        _xf("ia", "triangle", "incurred", 600.0, accident_year=2023,
            segment=seg, product=prod, subsegment="agency_channel"),
        _xf("id", "triangle", "incurred", 400.0, accident_year=2023,
            segment=seg, product=prod, subsegment="direct_channel"),
        _xf("p", "premiums", "premiums_earned_net", 2000.0,
            period_end="2023-12-31", segment=seg),
    ])
    detail = fs.derive_insurer("PGR")
    assert len([r for r in detail if r["grain"] == "product"]) == 2
    seg_row = next(r for r in detail if r["grain"] == "segment")
    assert seg_row["reported_claims"] == 100_000
    assert seg_row["incurred_musd"] == 1000.0
    assert seg_row["frequency_per_musd"] == 50.0


def test_unmatched_or_zero_count_cells_dropped(fresh_db):
    seg = "personal_lines_segment"
    db.upsert_xbrl_facts([
        # counts without incurred → no row; zero counts → no row.
        _xf("c1", "claim_counts", "reported_claims", 100, accident_year=2023,
            segment=seg, product="no_triangle_line", is_count=1),
        _xf("c2", "claim_counts", "reported_claims", 0, accident_year=2023,
            segment=seg, product="zero_line", is_count=1),
        _xf("i2", "triangle", "incurred", 10.0, accident_year=2023,
            segment=seg, product="zero_line"),
    ])
    assert fs.derive_insurer("PGR") == []


def test_fit_trend_recovers_geometric_growth():
    pairs = [(2019 + i, 10_000.0 * 1.05 ** i) for i in range(6)]
    t = fs.fit_trend(pairs)
    assert t["annual_trend"] == pytest.approx(0.05, abs=1e-4)
    assert t["n"] == 6 and t["r2"] == pytest.approx(1.0, abs=1e-6)


def test_fit_trend_guards():
    assert fs.fit_trend([(2023, 100.0), (2024, 110.0)]) is None        # < min points
    assert fs.fit_trend([(2022, -5.0), (2023, 0.0), (2024, 10.0)]) is None  # non-positive


def test_trend_rows_exclude_immature_latest_ay(fresh_db):
    """Severity grows 10%/yr over mature AYs; the noisy latest AY is ignored."""
    seg, prod = "personal_lines_segment", "auto_liability"
    facts = []
    for i, ay in enumerate(range(2021, 2025)):                # mature: 10%/yr
        facts.append(_xf(f"c{i}", "claim_counts", "reported_claims", 100_000,
                         accident_year=ay, segment=seg, product=prod, is_count=1))
        facts.append(_xf(f"i{i}", "triangle", "incurred",
                         1000.0 * 1.10 ** (ay - 2021),
                         accident_year=ay, segment=seg, product=prod))
    # Immature AY 2025 (as_of year): wildly low — would wreck the fit if included.
    facts.append(_xf("c9", "claim_counts", "reported_claims", 100_000,
                     accident_year=2025, segment=seg, product=prod, is_count=1))
    facts.append(_xf("i9", "triangle", "incurred", 100.0,
                     accident_year=2025, segment=seg, product=prod))
    db.upsert_xbrl_facts(facts)
    trends = fs.trend_rows(fs.derive_insurer("PGR"))
    prod_t = next(t for t in trends if t["grain"] == "product")
    assert prod_t["severity_trend"] == pytest.approx(0.10, abs=1e-3)
    assert prod_t["ay_span"] == "2021-2024"
    assert prod_t["latest_mature_ay"] == 2024


def test_trend_rows_loss_cost_combines_freq_and_sev(fresh_db):
    """Frequency -2%/yr × severity +10%/yr → loss-cost ≈ +7.8%/yr."""
    seg, prod = "personal_lines_segment", "auto_liability"
    facts = []
    for i, ay in enumerate(range(2021, 2025)):
        claims = 100_000 * 0.98 ** (ay - 2021)
        sev = 10_000.0 * 1.10 ** (ay - 2021)
        facts.append(_xf(f"c{i}", "claim_counts", "reported_claims", claims,
                         accident_year=ay, segment=seg, product=prod, is_count=1))
        facts.append(_xf(f"i{i}", "triangle", "incurred", claims * sev / 1e6,
                         accident_year=ay, segment=seg, product=prod))
        facts.append(_xf(f"p{i}", "premiums", "premiums_earned_net", 2000.0,
                         period_end=f"{ay}-12-31", segment=seg))
    db.upsert_xbrl_facts(facts)
    trends = fs.trend_rows(fs.derive_insurer("PGR"))
    seg_t = next(t for t in trends if t["grain"] == "segment")
    assert seg_t["frequency_trend"] == pytest.approx(-0.02, abs=1e-3)
    assert seg_t["severity_trend"] == pytest.approx(0.10, abs=1e-3)
    assert seg_t["loss_cost_trend"] == pytest.approx(1.10 * 0.98 - 1, abs=2e-3)
    assert seg_t["pure_premium_trend"] == pytest.approx(seg_t["loss_cost_trend"], abs=2e-3)


def test_trend_rows_yoy_fallback_with_short_ep_history(fresh_db):
    """Only 2 mature AYs with EP (the live 10-K shape) → trend None, YoY set."""
    _seed_basic()                                    # mature EP years: 2023, 2024
    trends = fs.trend_rows(fs.derive_insurer("PGR"))
    seg_t = next(t for t in trends if t["grain"] == "segment")
    assert seg_t["frequency_trend"] is None and seg_t["pure_premium_trend"] is None
    # freq: (100k/2200) / (100k/2000) - 1 ≈ -9.1%; pp: (1100/2200)/(1000/2000) = 0
    assert seg_t["frequency_yoy"] == pytest.approx(-0.0909, abs=1e-3)
    assert seg_t["pure_premium_yoy"] == pytest.approx(0.0, abs=1e-6)


def test_earned_premium_prefers_earned_net_and_max(fresh_db):
    seg, prod = "personal_lines_segment", "auto_liability"
    db.upsert_xbrl_facts([
        _xf("c", "claim_counts", "reported_claims", 100_000, accident_year=2023,
            segment=seg, product=prod, is_count=1),
        _xf("i", "triangle", "incurred", 1000.0, accident_year=2023,
            segment=seg, product=prod),
        # premium_revenue sibling + a smaller sub-cut on the same segment context:
        # premiums_earned_net wins over premium_revenue; max wins within a field.
        _xf("pr", "premiums", "premium_revenue", 2500.0,
            period_end="2023-12-31", segment=seg),
        _xf("pe", "premiums", "premiums_earned_net", 2000.0,
            period_end="2023-12-31", segment=seg),
        _xf("pe_cut", "premiums", "premiums_earned_net", 300.0,
            period_end="2023-12-31", segment=seg, product="some_subcut"),
    ])
    seg_row = next(r for r in fs.derive_insurer("PGR") if r["grain"] == "segment")
    assert seg_row["earned_premium_musd"] == 2000.0


def test_run_freq_sev_persists_and_loader_filters(fresh_db):
    _seed_basic()
    counts = fs.run_freq_sev()
    assert counts == {"insurers": 1, "rows": 6}          # 3 product + 3 segment AYs
    rows = db.freq_sev_detail("pgr")
    assert len(rows) == 6
    assert {r["grain"] for r in rows} == {"product", "segment"}
    assert db.freq_sev_detail("ZZZ") == []
    # Idempotent re-run.
    assert fs.run_freq_sev(["PGR"]) == {"insurers": 1, "rows": 6}
    assert len(db.freq_sev_detail("PGR")) == 6
