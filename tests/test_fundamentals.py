"""Insurer-fundamentals accessors over the XBRL registry + statutory facts."""
from __future__ import annotations

from digest import db, fundamentals as f

_XBRL_DIMS = dict(period_type="duration", accident_year=None, segment=None,
                  product=None, subsegment=None, geography=None,
                  investment_type=None, instrument=None, fv_level=None, is_count=0)


def _xf(fact_key, dataset, field, value, **kw):
    row = {"fact_key": fact_key, "insurer": "PGR", "dataset": dataset, "concept": "C",
           "field": field, "period_end": "2025-12-31", "value": value,
           "as_of": "2025-12-31", **_XBRL_DIMS}
    row.update(kw)
    return row


def _seed():
    db.upsert_xbrl_facts([
        _xf("a", "premiums", "premiums_earned_net", 50000.0),          # consolidated
        _xf("b", "claim_counts", "reported_claims", 100000.0,
            period_type="instant", accident_year=2024, product="personal_auto", is_count=1),
        _xf("c", "combined_ratio", "losses_and_lae_incurred", 30000.0, segment="personal"),
    ])
    db.upsert_triangle_cells([{
        "insurer": "PGR", "lob": "personal_lines_vehicles_direct_liability",
        "metric": "incurred", "accident_year": 2024, "dev_period": 12,
        "cumulative_value": 1000.0, "as_of": "2025-12-31"}])
    db.upsert_statutory_facts([
        {"insurer": "state_farm", "source": "iii", "dataset": "premiums",
         "field": "direct_premiums_written", "line": "personal_auto",
         "value": 67748.0, "period": "2023", "unit": "usd_millions"},
        {"insurer": "state_farm", "source": "iii", "dataset": "market_share",
         "field": "market_share", "line": "personal_auto",
         "value": 18.9, "period": "2023", "unit": "pct"},
    ])


def test_insurer_fundamentals(fresh_db):
    _seed()
    d = f.insurer_fundamentals("pgr")
    assert d["total_earned_premium_musd"] == 50000.0
    assert {x["dataset"] for x in d["datasets"]} >= {"premiums", "claim_counts", "combined_ratio"}
    assert any(t["canonical_lob"] == "personal_auto" for t in d["triangles_by_canonical_lob"])


def test_claim_counts_are_frequency_by_ay(fresh_db):
    _seed()
    cc = f.claim_counts_by_ay("PGR")
    assert cc and cc[0]["reported_claims"] == 100000.0 and cc[0]["accident_year"] == 2024


def test_combined_ratio_components(fresh_db):
    _seed()
    comps = f.combined_ratio_components("PGR")
    fields = {c["field"] for c in comps}
    assert "losses_and_lae_incurred" in fields and "premiums_earned_net" in fields


def test_statutory_top_writers_includes_mutuals(fresh_db):
    _seed()
    tw = f.statutory_top_writers("personal_auto")
    assert tw[0]["insurer"] == "state_farm"
    assert tw[0]["dpw_musd"] == 67748.0 and tw[0]["market_share_pct"] == 18.9


def test_combined_ratio_computes_loss_expense_combined(fresh_db):
    db.upsert_xbrl_facts([
        _xf("p", "premiums", "premiums_earned_net", 1000.0),
        _xf("l", "combined_ratio", "losses_and_lae_incurred", 660.0),
        _xf("l0", "combined_ratio", "losses_and_lae_incurred", 0.0),   # sibling 0 — magnitude wins
        _xf("e", "combined_ratio", "underwriting_expense", 140.0),
    ])
    d = f.combined_ratio("PGR")
    assert d["loss_lae_ratio"] == 0.66       # 660/1000, not the 0-valued sibling
    assert d["expense_ratio"] == 0.14
    assert d["combined_ratio"] == 0.80


def test_combined_ratio_gates_implausible_loss_line(fresh_db):
    db.upsert_xbrl_facts([
        _xf("p", "premiums", "premiums_earned_net", 1000.0),
        _xf("l", "combined_ratio", "losses_and_lae_incurred", 30.0),   # 3% → implausible
    ])
    d = f.combined_ratio("PGR")
    assert d["losses_lae_musd"] == 30.0      # raw value still surfaced
    assert d["loss_lae_ratio"] is None       # but the ratio is gated out
    assert d["combined_ratio"] is None


def test_combined_ratio_none_without_premium(fresh_db):
    d = f.combined_ratio("ZZZ")
    assert d["earned_premium_musd"] is None and d["combined_ratio"] is None
