"""Tests for the lifted runner mechanics in digest_core.summarize.runner."""
from __future__ import annotations

from digest_core.summarize import runner


# ── extract_json ────────────────────────────────────────────────────────


def test_extract_json_plain():
    assert runner.extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_fenced_block():
    raw = "here you go:\n```json\n{\"topic\": \"cyber\"}\n```\nthanks"
    assert runner.extract_json(raw) == {"topic": "cyber"}


def test_extract_json_embedded_in_prose():
    assert runner.extract_json('prose {"x": [1, 2]} trailing') == {"x": [1, 2]}


def test_extract_json_unparseable_returns_none():
    assert runner.extract_json("no json here at all") is None


# ── enforce_topic_caps ──────────────────────────────────────────────────


def _row(rid, topic, score):
    return {"id": rid, "topic": topic, "triage_score": score}


def test_enforce_topic_caps_trims_over_cap_topic_keeping_highest():
    # 8 ai_insurtech + 2 others; cap ai_insurtech at 35%.
    rows = [_row(i, "ai_insurtech", 1.0 - i * 0.01) for i in range(8)]
    rows += [_row(100, "cyber", 0.9), _row(101, "personal_lines", 0.9)]

    kept, dropped = runner.enforce_topic_caps(rows, {"ai_insurtech": 0.35})

    # max_allowed = int(0.35/0.65 * 2 others) = 1 → one ai_insurtech survives
    n_ai = sum(1 for r in kept if r["topic"] == "ai_insurtech")
    assert n_ai == 1
    assert dropped == {"ai_insurtech": 7}
    # the survivor is the highest-scored ai_insurtech (id 0); both others kept
    assert {r["id"] for r in kept} == {0, 100, 101}


def test_enforce_topic_caps_noop_when_under_cap():
    # 1 ai_insurtech of 4 = 25% ≤ 35% → nothing dropped
    rows = [
        _row(1, "ai_insurtech", 0.8), _row(2, "cyber", 0.9),
        _row(3, "cyber", 0.85), _row(4, "personal_lines", 0.7),
    ]
    kept, dropped = runner.enforce_topic_caps(rows, {"ai_insurtech": 0.35})
    assert len(kept) == 4 and dropped == {}


def test_enforce_topic_caps_empty_inputs():
    assert runner.enforce_topic_caps([], {"ai_insurtech": 0.35}) == ([], {})
    rows = [_row(1, "cyber", 0.9)]
    assert runner.enforce_topic_caps(rows, {}) == (rows, {})
