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
    assert signals._DEFAULT_WEIGHTS["signal_tiers"] == {"high": 1.6, "medium": 0.9}


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


def test_score_item_assigns_tier_consistent_with_score():
    class _Row(dict):
        def keys(self):
            return super().keys()

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
