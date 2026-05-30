"""Regression test for regime's summarizer-backend call.

compute_market_cycle must call the backend with the lifted 3-arg signature
(system_prompt, user_prompt, cfg) and pass regime's own system prompt. This
guards against the break where regime still called backend_fn(full) with the
pre-lift single-arg signature (which nothing else exercised).
"""
from __future__ import annotations

import json

from digest import regime, summarize


def _sig(market_cycle, cat_load, proposed=None):
    """Fake regime_signals row: held (effective) state + recorded proposal."""
    ev = {"proposed": {"market_cycle": proposed[0], "cat_load": proposed[1]}} if proposed else {}
    return {"market_cycle": market_cycle, "cat_load": cat_load,
            "evidence_json": json.dumps(ev)}


def test_hysteresis_no_history_confirms(monkeypatch):
    monkeypatch.setattr(regime.db, "recent_regime_signals", lambda n=1: [])
    assert regime._apply_hysteresis("hard_market", "low_season") == ("hard_market", "low_season", True)


def test_hysteresis_matches_active_state_confirms(monkeypatch):
    monkeypatch.setattr(regime.db, "recent_regime_signals",
                        lambda n=1: [_sig("hard_market", "post_major_event")])
    assert regime._apply_hysteresis("hard_market", "post_major_event")[2] is True


def test_hysteresis_first_disagreeing_proposal_is_pending(monkeypatch):
    # Stable hard baseline (its recorded proposal == itself); a fresh soft
    # proposal must NOT flip on the first reading — holds hard, pending.
    monkeypatch.setattr(regime.db, "recent_regime_signals",
        lambda n=1: [_sig("hard_market", "post_major_event",
                          proposed=("hard_market", "post_major_event"))])
    mc, cl, confirmed = regime._apply_hysteresis("transitioning_to_soft", "post_major_event")
    assert (mc, cl) == ("hard_market", "post_major_event")
    assert confirmed is False


def test_hysteresis_second_agreeing_proposal_confirms(monkeypatch):
    # Prior run HELD hard but RECORDED a soft proposal (pending). A second soft
    # proposal now confirms the transition — the bug was this never happening.
    monkeypatch.setattr(regime.db, "recent_regime_signals",
        lambda n=1: [_sig("hard_market", "post_major_event",
                          proposed=("transitioning_to_soft", "post_major_event"))])
    mc, cl, confirmed = regime._apply_hysteresis("transitioning_to_soft", "post_major_event")
    assert (mc, cl) == ("transitioning_to_soft", "post_major_event")
    assert confirmed is True


def test_hysteresis_flapping_proposal_stays_pending(monkeypatch):
    # Prior recorded proposal differs from the new one → noise, stays pending.
    monkeypatch.setattr(regime.db, "recent_regime_signals",
        lambda n=1: [_sig("hard_market", "post_major_event",
                          proposed=("soft_market", "low_season"))])
    assert regime._apply_hysteresis("transitioning_to_soft", "post_major_event")[2] is False


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
