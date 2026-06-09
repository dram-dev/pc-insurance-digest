"""Tests for the signal-leaderboard conviction tier (high/medium/low).

Adapted from macro-ai-digest's Signal tiering, but PC's score is an unbounded
product of multipliers (not clamped to [0,1]), so tiers anchor to a neutral
baseline and the thresholds are tunable via _meta/Scoring Weights.md.
"""
from __future__ import annotations

import pytest

from digest import signals


@pytest.fixture(autouse=True)
def _reset_weights_cache():
    """Each test sees a clean weights cache so vault overrides don't leak."""
    signals._WEIGHTS_CACHE = None
    yield
    signals._WEIGHTS_CACHE = None


def test_tier_for_score_boundaries():
    high = signals.SIGNAL_TIER_DEFAULTS["high"]
    medium = signals.SIGNAL_TIER_DEFAULTS["medium"]
    assert signals.tier_for_score(high) == "high"          # inclusive lower bound
    assert signals.tier_for_score(high + 1.0) == "high"
    assert signals.tier_for_score(medium) == "medium"      # inclusive lower bound
    assert signals.tier_for_score((high + medium) / 2) == "medium"
    assert signals.tier_for_score(medium - 0.001) == "low"
    assert signals.tier_for_score(0.0) == "low"


def test_tier_for_score_none_passthrough():
    assert signals.tier_for_score(None) is None


def test_tier_badge_text():
    assert signals.tier_badge(signals.SIGNAL_TIER_DEFAULTS["high"]) == "🔴 High"
    assert signals.tier_badge(signals.SIGNAL_TIER_DEFAULTS["medium"]) == "🟡 Medium"
    assert signals.tier_badge(0.0) == "🔵 Low"
    assert signals.tier_badge(None) == ""


def test_tier_thresholds_overridable_via_vault(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    meta = vault / "81 P&C Digest" / "_meta"
    meta.mkdir(parents=True)
    (meta / "Scoring Weights.md").write_text(
        "---\nsignal_tiers:\n  high: 3.0\n  medium: 2.0\n---\n\nbody\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(signals.settings, "obsidian_vault_path", str(vault))
    monkeypatch.setattr(signals.settings, "obsidian_digest_dir", "81 P&C Digest")

    assert signals.tier_thresholds() == (3.0, 2.0)
    assert signals.tier_for_score(3.0) == "high"
    assert signals.tier_for_score(2.5) == "medium"   # below new high, >= new medium
    assert signals.tier_for_score(1.9) == "low"      # would be "medium" under defaults


def test_default_weights_includes_signal_tiers():
    st = signals._DEFAULT_WEIGHTS["signal_tiers"]
    assert st["high"] == 1.6 and st["medium"] == 0.9          # fixed fallbacks
    assert st["high_quantile"] == 0.90 and st["medium_quantile"] == 0.60
    assert st["min_n"] == 80                                   # quantile gate


def test_default_weights_includes_insurer_names():
    assert signals._DEFAULT_WEIGHTS["insurer_names"]["state farm"] == 1.5
    assert signals._DEFAULT_WEIGHTS["insurer_names"]["allstate"] == 1.5


def test_insurer_name_boost_fires_for_state_farm_on_any_source():
    # State Farm is a mutual — no EDGAR ticker ever — so the name path is its
    # only route to a carrier-priority weighting.
    b = signals._insurer_priority_boost(
        "insurance_journal", None, blob="State Farm Mutual seeks 12% auto rate hike in CA")
    assert b == 1.5


def test_insurer_name_boost_fires_for_allstate_trade_press():
    # Allstate's ticker boost only covers its 8-Ks; the name path covers the rest.
    b = signals._insurer_priority_boost(
        "reinsurance_news", None, blob="Allstate reports higher catastrophe losses")
    assert b == 1.5


def test_insurer_ticker_and_name_combine_as_max_not_product():
    # An Allstate 8-K whose summary also says "Allstate" must not double-count.
    b = signals._insurer_priority_boost(
        "edgar", '{"ticker": "ALL"}', blob="Allstate 8-K: quarterly results")
    assert b == 1.5


def test_insurer_name_boost_neutral_without_carrier():
    assert signals._insurer_priority_boost("hackernews", None, blob="generic insurtech news") == 1.0
    # No blob (older 3-arg callers) → name path is a no-op, ticker path unchanged.
    assert signals._insurer_priority_boost("edgar", '{"ticker": "PGR"}') == 1.5


def test_insurer_names_overridable_via_vault(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    meta = vault / "81 P&C Digest" / "_meta"
    meta.mkdir(parents=True)
    (meta / "Scoring Weights.md").write_text(
        "---\ninsurer_names:\n  state farm: 1.8\n---\nbody\n", encoding="utf-8")
    monkeypatch.setattr(signals.settings, "obsidian_vault_path", str(vault))
    monkeypatch.setattr(signals.settings, "obsidian_digest_dir", "81 P&C Digest")
    w = signals._load_scoring_weights()
    assert w["insurer_names"]["state farm"] == 1.8      # overridden
    assert w["insurer_names"]["allstate"] == 1.5         # default preserved


class _Row(dict):
    def keys(self):
        return super().keys()


def _regime():
    return type("_Regime", (), {"multiplier": 1.0})()


# ── keyword de-dup + stack cap ─────────────────────────────────────────────


def test_litigation_phrases_fire_tplf_family_only():
    # "nuclear verdict" used to hit BOTH keyword regexes — 1.2 × 1.3 on top of
    # social_inflation's 1.4 topic boost, a 2.18× stack from one phrase counted
    # three times. The families are now disjoint.
    for phrase in ("nuclear verdict in Georgia", "tort reform stalls",
                   "social inflation accelerates", "record settlement announced"):
        assert signals._INFLATION_RE.search(phrase) is None, phrase
        assert signals._TPLF_RE.search(phrase), phrase
    # Pure cost drivers still belong to the inflation family only.
    for phrase in ("auto parts inflation", "body shop labor rate", "used-car prices"):
        assert signals._INFLATION_RE.search(phrase), phrase
        assert signals._TPLF_RE.search(phrase) is None, phrase


def test_keyword_stack_cap_and_factor_product_invariant():
    # One item hitting all three keyword families: raw stack would be
    # 1.2 × 1.2 × 1.3 = 1.872 — the cap scales all three back to ≤1.6,
    # proportionally, so the persisted factors still multiply to the score.
    row = _Row(
        id=1, source="rss", topic="cyber",
        published_at=None, ingested_at="2026-06-09T00:00:00+00:00",
        title="FAIR Plan rate filing follows nuclear verdict; auto parts inflation bites",
        materiality_score=1.0,
    )
    s = signals.score_item(row, _regime())
    stack = s.inflation_boost * s.regulatory_boost * s.tplf_boost
    # ≈ the cap (persisted factors are rounded to 3 decimals, so allow rounding).
    assert stack == pytest.approx(signals.KEYWORD_STACK_CAP_DEFAULT, abs=0.01)
    product = (s.source_mult * s.regime_mult * s.topic_relevance * s.recency
               * s.llm_judgment * s.topic_boost * s.burden_boost * s.insurer_boost
               * s.inflation_boost * s.regulatory_boost * s.tplf_boost * s.reserve_boost)
    assert s.score == pytest.approx(product, rel=0.01)  # rounding only


def test_single_family_hit_not_capped():
    row = _Row(
        id=1, source="rss", topic="cyber",
        published_at=None, ingested_at="2026-06-09T00:00:00+00:00",
        title="auto parts inflation bites", materiality_score=1.0,
    )
    s = signals.score_item(row, _regime())
    assert s.inflation_boost == pytest.approx(1.2)
    assert s.regulatory_boost == 1.0 and s.tplf_boost == 1.0


# ── exponential per-topic recency ──────────────────────────────────────────


def _iso_days_ago(days: float) -> str:
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def test_recency_half_life_property_per_topic():
    # At exactly one half-life the decay is 0.5 — for each topic's own h.
    assert signals._recency(_iso_days_ago(2.0), None, topic="cat_event") == pytest.approx(0.5, abs=0.01)
    assert signals._recency(_iso_days_ago(7.0), None, topic="cyber") == pytest.approx(0.5, abs=0.01)
    assert signals._recency(_iso_days_ago(14.0), None, topic="regulatory_rate") == pytest.approx(0.5, abs=0.01)
    assert signals._recency(_iso_days_ago(21.0), None, topic="reserving") == pytest.approx(0.5, abs=0.01)


def test_recency_topic_ordering_at_fixed_age():
    # Same 7-day-old item: stale if cat_event, half-live if default, fresher if reserving.
    cat = signals._recency(_iso_days_ago(7.0), None, topic="cat_event")
    default = signals._recency(_iso_days_ago(7.0), None, topic="cyber")
    res = signals._recency(_iso_days_ago(7.0), None, topic="reserving")
    assert cat < default < res


def test_recency_floor_and_missing_timestamp():
    assert signals._recency(_iso_days_ago(365.0), None, topic="cat_event") == 0.1
    assert signals._recency(None, None) == 0.6
    assert signals._recency("not-a-date", None) == 0.6


def test_recency_half_lives_overridable_via_vault(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    meta = vault / "81 P&C Digest" / "_meta"
    meta.mkdir(parents=True)
    (meta / "Scoring Weights.md").write_text(
        "---\nrecency_half_lives:\n  cat_event: 10.0\n---\nbody\n", encoding="utf-8")
    monkeypatch.setattr(signals.settings, "obsidian_vault_path", str(vault))
    monkeypatch.setattr(signals.settings, "obsidian_digest_dir", "81 P&C Digest")
    w = signals._load_scoring_weights()
    assert w["recency_half_lives"]["cat_event"] == 10.0      # overridden
    assert w["recency_half_lives"]["reserving"] == 21.0      # default preserved


# ── quantile-calibrated tiers ──────────────────────────────────────────────


def test_quantile_thresholds_fixed_below_min_n(fresh_db):
    high, medium, basis = signals.quantile_tier_thresholds()
    assert basis == "fixed"
    assert (high, medium) == (signals.SIGNAL_TIER_DEFAULTS["high"],
                              signals.SIGNAL_TIER_DEFAULTS["medium"])


def _seed_scored(make_item, n):
    from digest import db
    for i in range(n):
        sid = f"q{i}"
        db.upsert_items([make_item(source="rss", source_id=sid, title=f"t{i}")])
        with db.get_conn() as conn:
            iid = conn.execute("SELECT id FROM items WHERE source_id=?", (sid,)).fetchone()["id"]
        db.upsert_signal_scores([{
            "item_id": iid, "computed_at": "2026-06-01T00:00:00",
            "score": (i + 1) / n,                       # 0.01 .. 1.00
            "source_mult": 1.0, "regime_mult": 1.0, "topic_relevance": 1.0,
            "recency": 1.0, "llm_judgment": 1.0, "topic_boost": 1.0,
            "burden_boost": 1.0, "insurer_boost": 1.0, "inflation_boost": 1.0,
            "regulatory_boost": 1.0, "tplf_boost": 1.0, "tier": "low",
        }])


def test_quantile_thresholds_from_trailing_distribution(fresh_db, make_item):
    _seed_scored(make_item, n=100)
    high, medium, basis = signals.quantile_tier_thresholds()
    assert basis == "quantile"
    # Scores are 0.01..1.00: interpolated P90 = 0.901, P60 = 0.604.
    assert high == pytest.approx(0.901, abs=0.002)
    assert medium == pytest.approx(0.604, abs=0.002)
    assert high > medium


def test_run_signals_self_calibrates_tiers_on_second_run(fresh_db, make_item):
    # First run: no score history → fixed cutoffs. Second run: the first run's
    # 100 scores are the trailing distribution → quantile cutoffs.
    from digest import db
    for i in range(100):
        sid = f"r{i}"
        db.upsert_items([make_item(source="rss", source_id=sid, title=f"item {i}")])
        with db.get_conn() as conn:
            iid = conn.execute("SELECT id FROM items WHERE source_id=?", (sid,)).fetchone()["id"]
            conn.execute(
                "UPDATE items SET triage_decision='keep', summary='s', materiality_score=? WHERE id=?",
                (0.5 + (i / 99.0), iid))                 # materiality 0.5..1.5 → score spread
    r1 = signals.run_signals()
    assert r1["scored"] == 100 and r1["tier_basis"] == "fixed"
    r2 = signals.run_signals()
    assert r2["tier_basis"] == "quantile"
    assert r2["tier_high"] > r2["tier_medium"]
    # Persisted tier matches the run's own cutoffs.
    with db.get_conn() as conn:
        rows = conn.execute("""
            WITH latest AS (SELECT item_id, MAX(computed_at) c FROM signal_scores GROUP BY item_id)
            SELECT s.score, s.tier FROM signal_scores s
            JOIN latest l ON l.item_id = s.item_id AND l.c = s.computed_at""").fetchall()
    for r in rows:
        assert r["tier"] == signals.tier_for_score(r["score"], r2["tier_high"], r2["tier_medium"])


def test_tier_badge_for_row_prefers_persisted_tier():
    # A row scored under quantile cutoffs may carry a tier the fixed mapping
    # would disagree with — the persisted tier wins.
    assert signals.tier_badge_for_row(_Row(tier="high", score=0.1)) == "🔴 High"
    assert signals.tier_badge_for_row(_Row(score=2.0)) == "🔴 High"   # fallback path
    assert signals.tier_badge_for_row(_Row(title="no score")) == ""


def test_score_item_assigns_tier_consistent_with_score():
    regime = type("_Regime", (), {"multiplier": 1.0})()
    row = _Row(
        id=1, source="edgar", topic="social_inflation",
        published_at=None, ingested_at="2026-05-28T00:00:00+00:00",
        materiality_score=1.5, burden_intensity="high",
        metadata_json='{"ticker": "PGR"}',
    )
    s = signals.score_item(row, regime)
    # tier the scorer stamps matches what the public mapper says for that score
    assert s.tier in ("high", "medium", "low")
    assert s.tier == signals.tier_for_score(s.score)
    assert s.as_row("2026-05-28T00:00:00+00:00")["tier"] == s.tier
