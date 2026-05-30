"""InsurTech Capital-Flow (EKG Lead 8) — deal extraction + cap protection.

Network-free: drives the regex extractor and the protected-cap wrapper directly,
plus a run over synthetic queue rows.
"""
from __future__ import annotations

from digest import capital_flows, db
from digest.summarize import TOPIC_CAP_PCT


def test_extract_deal_funding_round_with_amount():
    d = capital_flows.extract_deal("InsurTech MGA raises $120 million Series B round")
    assert d["deal_type"] == "funding_round"
    assert d["amount_usd"] == 120_000_000.0
    assert d["stage"] == "series_b"


def test_extract_deal_billions_and_ma():
    d = capital_flows.extract_deal("Broker to acquire rival for $1.5 billion")
    assert d["deal_type"] == "m&a"
    assert d["amount_usd"] == 1_500_000_000.0


def test_extract_deal_unsubstantiated_funding_has_no_amount():
    d = capital_flows.extract_deal("Startup announces new AI underwriting funding round")
    assert d["deal_type"] == "funding_round"
    assert d["amount_usd"] is None          # no dollar figure → not cap-protected


def test_extract_deal_non_deal_returns_none():
    assert capital_flows.extract_deal("AI model benchmarks improve on vision tasks") is None
    assert capital_flows.extract_deal("") is None


class _Row(dict):
    """dict that also supports row['col'] — matches sqlite3.Row access in the code."""


def _row(i, topic, title, content="", triage=0.5):
    return _Row(id=i, source="rss", source_id=f"s{i}", title=title,
                content=content, topic=topic, triage_score=triage)


def test_run_capital_flows_protects_substantiated_only(fresh_db):
    rows = [
        _row(1, "ai_insurtech", "MGA raises $50 million Series A"),
        _row(2, "ai_insurtech", "Startup closes new funding round, amount undisclosed"),  # deal, no amount
        _row(3, "cyber", "Carrier buys reinsurer for $2 billion"),     # wrong topic
    ]
    protected = capital_flows.run_capital_flows(rows)
    assert protected == {1}                                            # only the $-amount ai_insurtech deal
    # Persisted both ai_insurtech deals (item 1 substantiated, item 2 not).
    with db.get_conn() as conn:
        n = conn.execute("SELECT COUNT(*) FROM capital_flows").fetchone()[0]
        amt1 = conn.execute("SELECT amount_usd FROM capital_flows WHERE item_id=1").fetchone()[0]
    assert n == 2
    assert amt1 == 50_000_000.0


def test_protected_cap_keeps_substantiated_deal_over_the_share_limit(fresh_db):
    # 10 items: 7 ai_insurtech (way over the 35% cap) + 3 others. One ai_insurtech
    # item carries a real deal and must survive even though the cap culls the topic.
    rows = [_row(i, "ai_insurtech", f"insurtech blurb {i}", triage=0.1 + i / 100)
            for i in range(1, 8)]
    rows += [_row(i, "cyber", f"cyber {i}", triage=0.9) for i in range(8, 11)]
    # Item 1 is the lowest-scored ai_insurtech but is a substantiated deal.
    rows[0] = _row(1, "ai_insurtech", "MGA raises $40 million seed", triage=0.01)

    protected = capital_flows.run_capital_flows(rows)
    assert 1 in protected
    kept, dropped = capital_flows.enforce_topic_caps_protected(rows, TOPIC_CAP_PCT, protected)
    kept_ids = {r["id"] for r in kept}
    assert 1 in kept_ids                                  # protected deal survived the cap
    assert dropped.get("ai_insurtech", 0) > 0             # the topic was still capped


def test_protected_cap_delegates_when_nothing_protected(fresh_db):
    rows = [_row(i, "ai_insurtech", f"blurb {i}", triage=i / 10) for i in range(1, 8)]
    rows += [_row(i, "cyber", f"cyber {i}", triage=0.9) for i in range(8, 11)]
    kept, dropped = capital_flows.enforce_topic_caps_protected(rows, TOPIC_CAP_PCT, set())
    # Same result as the plain core cap.
    from digest_core.summarize.runner import enforce_topic_caps
    base_kept, base_dropped = enforce_topic_caps(rows, TOPIC_CAP_PCT)
    assert {r["id"] for r in kept} == {r["id"] for r in base_kept}
    assert dropped == base_dropped
