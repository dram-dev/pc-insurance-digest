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
