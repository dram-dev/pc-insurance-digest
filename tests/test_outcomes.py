"""Option 1b — outcome backtest: matcher, σ bands, stock-move math, run job."""
from __future__ import annotations

from datetime import datetime, timedelta

from digest import db, outcomes


# ── insurer matcher ──────────────────────────────────────────────────────


def test_match_insurer_by_name():
    assert outcomes.match_insurer("Progressive raises auto rates 12%") == "PGR"
    assert outcomes.match_insurer("GEICO expands telematics") == "BRK"
    assert outcomes.match_insurer("The Hartford posts combined ratio") == "HIG"


def test_match_insurer_no_false_positive_on_common_words():
    # bare tickers like ALL / EG are NOT aliases, so prose doesn't match.
    assert outcomes.match_insurer("all drivers face higher premiums, e.g. in CA") is None


# ── sigma bands ────────────────────────────────────────────────────────────


def test_sigma_band_buckets():
    assert outcomes.sigma_band(0.4) is None
    assert outcomes.sigma_band(0.5) == "0.5-0.75"
    assert outcomes.sigma_band(1.0) == "1.0-1.25"
    assert outcomes.sigma_band(1.9) == "1.75-2.0"
    assert outcomes.sigma_band(2.5) == "2+"


# ── stock-move math (synthetic deterministic closes) ─────────────────────


def _closes(start: str, days_before: int, jump_to: float | None) -> dict[str, float]:
    base = datetime.fromisoformat(start)
    out: dict[str, float] = {}
    for i in range(days_before, -8, -1):           # ... start-N .. start+7
        d = (base - timedelta(days=i)).strftime("%Y-%m-%d")
        out[d] = 100.0 + (i % 2)                    # oscillate 100/101 → σ>0
    if jump_to is not None:
        end = (base + timedelta(days=7)).strftime("%Y-%m-%d")
        out[end] = jump_to
    return out


def test_compute_stock_move_big_up_move():
    closes = _closes("2026-03-01", days_before=90, jump_to=130.0)
    z, band = outcomes.compute_stock_move(closes, "2026-03-01T00:00:00", 7)
    assert z is not None and z > 0 and band == "2+"


def test_compute_stock_move_insufficient_history():
    closes = {"2026-03-01": 100.0, "2026-03-08": 105.0}   # <20 trailing points
    assert outcomes.compute_stock_move(closes, "2026-03-01T00:00:00", 7) == (None, None)


# ── run_outcomes integration ──────────────────────────────────────────────


def _matured_item(make_item, source, sid, title, days_ago, *, topic=None,
                  scored=False, summary="s"):
    db.upsert_items([make_item(source=source, source_id=sid, title=title, content="c")])
    with db.get_conn() as conn:
        iid = conn.execute("SELECT id FROM items WHERE source_id=?", (sid,)).fetchone()["id"]
        ingested = (datetime.utcnow() - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "UPDATE items SET triage_decision='keep', ingested_at=?, topic=?, summary=? WHERE id=?",
            (ingested, topic, summary, iid))
    if scored:
        db.upsert_signal_scores([{
            "item_id": iid, "computed_at": "2026-01-01T00:00:00", "score": 2.0,
            "source_mult": 1.0, "regime_mult": 1.0, "topic_relevance": 1.0,
            "recency": 1.0, "llm_judgment": 1.0, "topic_boost": 1.0, "burden_boost": 1.0,
            "insurer_boost": 1.0, "inflation_boost": 1.0, "regulatory_boost": 1.0,
            "tplf_boost": 1.0, "tier": "high",
        }])
    return iid


def test_run_outcomes_corroborates(fresh_db, make_item, monkeypatch):
    monkeypatch.setattr(outcomes, "fetch_daily_closes", lambda t: {})   # stock off

    # X: scored, insurer-named, regulatory_rate, 40d ago.
    x = _matured_item(make_item, "rss", "x", "Progressive raises auto rates",
                      40, topic="regulatory_rate", scored=True)
    # follow-on: same topic, 5d after X (within 7d window), unscored.
    _matured_item(make_item, "rss", "follow", "Progressive rate filing approved",
                  35, topic="regulatory_rate")
    # EDGAR filing from PGR, 3d after X.
    _matured_item(make_item, "edgar", "PGR:acc1", "PGR 8-K", 37)
    db.upsert_manual_rating(x, 5.0)

    checked = outcomes.run_outcomes(horizons=(7,))
    assert checked[7] >= 1
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM outcome_backtest WHERE item_id=? AND horizon_days=7", (x,)
        ).fetchone()
    import json
    sigs = set(json.loads(row["signals_json"]))
    assert row["corroborated"] == 1
    assert {"followon", "edgar", "manual"} <= sigs
    assert row["edgar_filed"] == 1 and row["followon_count"] >= 1


def test_run_outcomes_records_stock_band(fresh_db, make_item, monkeypatch):
    monkeypatch.setattr(outcomes, "compute_stock_move", lambda c, t, h: (2.3, "2+"))
    monkeypatch.setattr(outcomes, "fetch_daily_closes", lambda t: {"x": 1.0})
    x = _matured_item(make_item, "rss", "x", "Allstate cuts homeowners exposure",
                      40, topic="personal_lines", scored=True)
    outcomes.run_outcomes(horizons=(30,))
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM outcome_backtest WHERE item_id=? AND horizon_days=30", (x,)
        ).fetchone()
    assert row["stock_move_band"] == "2+" and row["stock_move_z"] == 2.3
    assert "stock_move" in row["signals_json"]


def test_run_outcomes_skips_already_checked(fresh_db, make_item, monkeypatch):
    monkeypatch.setattr(outcomes, "fetch_daily_closes", lambda t: {})
    _matured_item(make_item, "rss", "x", "Plain item", 40, topic="cyber", scored=True)
    assert outcomes.run_outcomes(horizons=(7,))[7] == 1
    assert outcomes.run_outcomes(horizons=(7,))[7] == 0    # nothing left to check
