"""Regime detector — Markov-switching filter properties + backend-call regression.

The PR4 forward filter must reproduce the old hysteresis BEHAVIOR as posterior
dynamics: one contrarian LLM reading can't flip the reported state, two
consecutive ones can, and with no observation the posterior only diffuses.
compute_market_cycle must call the backend with the lifted 3-arg signature
(system_prompt, user_prompt, cfg) and pass regime's own system prompt.
"""
from __future__ import annotations

import json

import pytest

from digest import regime, summarize
from digest.regime import (
    MARKET_CYCLES,
    market_cycle_filter,
    posterior_multiplier,
    _prior_posterior,
)


def _one_hot(state: str) -> list[float]:
    return [1.0 if s == state else 0.0 for s in MARKET_CYCLES]


def _mode(posterior: list[float]) -> str:
    return MARKET_CYCLES[max(range(len(posterior)), key=posterior.__getitem__)]


# ── filter dynamics (the hysteresis property, now structural) ─────────────


def test_kernel_rows_are_distributions():
    for matrix in (regime.TRANSITION, regime.EMISSION):
        for row in matrix:
            assert sum(row) == pytest.approx(1.0)
            assert all(v > 0 for v in row)


def test_single_contrarian_reading_does_not_flip_mode():
    pi = market_cycle_filter(_one_hot("stable"), ["hard_market"])
    assert _mode(pi) == "stable"                  # sticky prior holds
    assert pi[MARKET_CYCLES.index("hard_market")] > 0.05   # but mass shifted


def test_two_consecutive_readings_flip_mode():
    pi = market_cycle_filter(_one_hot("stable"), ["hard_market"])
    pi = market_cycle_filter(pi, ["hard_market"])
    assert _mode(pi) == "hard_market"             # matches the old 2-agree rule


def test_no_observation_is_a_pure_predict_step():
    pi = market_cycle_filter(_one_hot("hard_market"), [])
    assert _mode(pi) == "hard_market"
    assert pi[MARKET_CYCLES.index("hard_market")] == pytest.approx(
        regime.TRANSITION[MARKET_CYCLES.index("hard_market")][MARKET_CYCLES.index("hard_market")])


def test_agreeing_emissions_sharpen_the_posterior():
    one = market_cycle_filter(_one_hot("stable"), ["hard_market"])
    two = market_cycle_filter(_one_hot("stable"), ["hard_market", "hard_market"])
    ih = MARKET_CYCLES.index("hard_market")
    assert two[ih] > one[ih]                      # the priced hint corroborating the LLM


def test_unknown_observation_is_ignored():
    pi = market_cycle_filter(_one_hot("stable"), ["not_a_state"])
    assert _mode(pi) == "stable"


def test_posterior_multiplier_is_expected_value_and_continuous():
    assert posterior_multiplier(_one_hot("hard_market")) == pytest.approx(1.20)
    mixed = market_cycle_filter(_one_hot("stable"), ["hard_market"])
    mult = posterior_multiplier(mixed)
    assert 1.00 < mult < 1.20                     # smooth glide, not a cliff


def test_prior_posterior_prefers_stored_then_one_hot_then_stable():
    stored = {"posterior": {s: (0.6 if s == "soft_market" else 0.1) for s in MARKET_CYCLES}}
    row = {"market_cycle": "hard_market", "evidence_json": json.dumps(stored)}
    pi = _prior_posterior(row)
    assert _mode(pi) == "soft_market"             # stored posterior wins
    assert sum(pi) == pytest.approx(1.0)
    legacy = {"market_cycle": "hard_market", "evidence_json": json.dumps({})}
    assert _prior_posterior(legacy) == _one_hot("hard_market")   # pre-PR4 row
    assert _prior_posterior(None) == _one_hot("stable")


def test_compute_market_cycle_calls_backend_with_three_args(monkeypatch):
    rows = [
        {
            "published_at": None, "ingested_at": "2026-05-20T00:00:00+00:00",
            "title": f"Carrier {i} Q1", "summary": "combined ratio rising",
            "why_it_matters": "capacity tightening", "topic": "underwriting_results",
        }
        for i in range(6)  # ≥5 so it doesn't short-circuit to 'stable'
    ]
    monkeypatch.setattr(regime.db, "items_for_market_cycle", lambda window_days=60: rows)

    captured = {}

    def fake_backend(system_prompt, user_prompt, cfg):
        captured["system"] = system_prompt
        captured["user"] = user_prompt
        return (
            '{"market_cycle": "hard_market", "combined_ratio_dir": "rising", '
            '"capacity_tone": "tight", "evidence": "test evidence"}'
        )

    monkeypatch.setitem(summarize.BACKENDS, summarize.settings.summarizer_backend, fake_backend)

    out = regime.compute_market_cycle()

    assert captured["system"] == regime.MARKET_CYCLE_SYSTEM_PROMPT  # regime's own prompt, not summarize's
    assert "Trailing window: 6 items." in captured["user"]
    assert out["market_cycle"] == "hard_market"
    assert out["n_items"] == 6
