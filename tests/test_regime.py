"""Regression test for regime's summarizer-backend call.

compute_market_cycle must call the backend with the lifted 3-arg signature
(system_prompt, user_prompt, cfg) and pass regime's own system prompt. This
guards against the break where regime still called backend_fn(full) with the
pre-lift single-arg signature (which nothing else exercised).
"""
from __future__ import annotations

from digest import regime, summarize


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
