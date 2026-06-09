"""Option 1b — outcome backtest: matcher, σ bands, stock-move math, run job."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

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


def _bench_closes(start: str, days_before: int, jump_to: float | None) -> dict[str, float]:
    """Benchmark series on the same dates: 200-level, quarter the insurer's
    relative oscillation (so excess daily returns have nonzero vol)."""
    base = datetime.fromisoformat(start)
    out: dict[str, float] = {}
    for i in range(days_before, -8, -1):
        d = (base - timedelta(days=i)).strftime("%Y-%m-%d")
        out[d] = 200.0 + 0.5 * (i % 2)
    if jump_to is not None:
        out[(base + timedelta(days=7)).strftime("%Y-%m-%d")] = jump_to
    return out


def test_market_wide_move_nets_out_against_benchmark():
    # The insurer jumps +30% — but so does the whole sector. The raw own-vol z is
    # huge; the benchmark-excess z must be small (it's beta, not the item).
    closes = _closes("2026-03-01", days_before=90, jump_to=130.0)
    bench = _bench_closes("2026-03-01", days_before=90, jump_to=260.0)  # +30% too
    z_raw, _ = outcomes.compute_stock_move(closes, "2026-03-01T00:00:00", 7)
    z_exc, _ = outcomes.compute_stock_move(closes, "2026-03-01T00:00:00", 7, bench)
    assert z_raw is not None and abs(z_raw) > 2.0
    assert z_exc is not None and abs(z_exc) < abs(z_raw) / 2


def test_idiosyncratic_move_survives_benchmark_adjustment():
    # The insurer jumps +30% while the benchmark stays flat — the excess z
    # must remain extreme.
    closes = _closes("2026-03-01", days_before=90, jump_to=130.0)
    bench = _bench_closes("2026-03-01", days_before=90, jump_to=200.5)  # flat horizon
    z_exc, band = outcomes.compute_stock_move(closes, "2026-03-01T00:00:00", 7, bench)
    assert z_exc is not None and abs(z_exc) > 2.0 and band == "2+"


# ── p-values + BH-FDR ─────────────────────────────────────────────────────


def test_two_sided_p_sanity():
    assert outcomes.two_sided_p(0.0) == pytest.approx(1.0)
    assert outcomes.two_sided_p(1.96) == pytest.approx(0.05, abs=0.001)
    assert outcomes.two_sided_p(-1.96) == pytest.approx(0.05, abs=0.001)  # symmetric
    # The old fixed trigger: a 1σ move is a ~32% event under the null.
    assert outcomes.two_sided_p(1.0) == pytest.approx(0.317, abs=0.001)


def test_bh_survivors_single_item_reduces_to_p_below_q():
    assert outcomes.bh_survivors({1: 0.02}, q=0.10) == {1}
    assert outcomes.bh_survivors({1: 0.20}, q=0.10) == set()
    assert outcomes.bh_survivors({}, q=0.10) == set()


def test_bh_survivors_step_up_property():
    # Classic BH: ranked p ≤ q·k/m. With q=0.10 and m=5: 0.01 ≤ 0.02 (k=1) and
    # 0.04 ≤ 0.04 (k=2) pass; 0.30/0.50/0.90 fail. The step-up keeps everything
    # at/below the largest passing rank.
    p = {1: 0.01, 2: 0.04, 3: 0.30, 4: 0.50, 5: 0.90}
    assert outcomes.bh_survivors(p, q=0.10) == {1, 2}


def test_bh_survivors_modest_z_cohort_all_rejected():
    # Five 1σ movers (p≈0.317 each): the old rule corroborated all five; BH at
    # q=0.10 corroborates none.
    p1 = outcomes.two_sided_p(1.0)
    assert outcomes.bh_survivors({i: p1 for i in range(5)}, q=0.10) == set()


# ── run_outcomes integration ──────────────────────────────────────────────


def _matured_item(make_item, source, sid, title, days_ago, *, topic=None,
                  scored=False, summary="s"):
    db.upsert_items([make_item(source=source, source_id=sid, title=title, content="c")])
    with db.get_conn() as conn:
        iid = conn.execute("SELECT id FROM items WHERE source_id=?", (sid,)).fetchone()["id"]
        ingested = (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")
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


def test_modest_stock_move_does_not_corroborate_under_fdr(fresh_db, make_item, monkeypatch):
    # The old fixed 1.0σ trigger fired on a ~32%-probability-under-null move.
    # Under BH-FDR (q=0.10) a lone 1σ move is recorded but does NOT corroborate.
    monkeypatch.setattr(outcomes, "compute_stock_move", lambda c, t, h: (1.0, "1.0-1.25"))
    monkeypatch.setattr(outcomes, "fetch_daily_closes", lambda t: {"x": 1.0})
    monkeypatch.setattr(outcomes, "_followon_count", lambda *a: 0)
    x = _matured_item(make_item, "rss", "x", "Allstate trims homeowners exposure",
                      40, topic="personal_lines", scored=True)
    outcomes.run_outcomes(horizons=(7,))
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM outcome_backtest WHERE item_id=? AND horizon_days=7", (x,)
        ).fetchone()
    assert row["stock_move_z"] == 1.0                       # recorded …
    assert row["stock_move_p"] == pytest.approx(0.317, abs=0.001)
    assert "stock_move" not in row["signals_json"]          # … but not corroborating
    assert row["corroborated"] == 0


def test_run_outcomes_skips_already_checked(fresh_db, make_item, monkeypatch):
    monkeypatch.setattr(outcomes, "fetch_daily_closes", lambda t: {})
    _matured_item(make_item, "rss", "x", "Plain item", 40, topic="cyber", scored=True)
    assert outcomes.run_outcomes(horizons=(7,))[7] == 1
    assert outcomes.run_outcomes(horizons=(7,))[7] == 0    # nothing left to check


# ── price feed (multi-source) ─────────────────────────────────────────────


def test_fetch_yahoo_parses_chart(monkeypatch):
    class FakeResp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            return {"chart": {"result": [{
                "timestamp": [1704067200, 1704153600],          # 2024-01-01, -02 UTC
                "indicators": {"quote": [{"close": [100.0, None]}]},  # None close skipped
            }]}}

    class FakeSession:
        def get(self, *a, **k): return FakeResp()

    monkeypatch.setattr(outcomes, "_yahoo_blocked", False)
    monkeypatch.setattr(outcomes, "_get_yahoo_session", lambda: (FakeSession(), None))
    assert outcomes._fetch_yahoo("PGR") == {"2024-01-01": 100.0}


def test_fetch_daily_closes_falls_back_to_stooq(monkeypatch):
    def boom(_t): raise RuntimeError("429 rate limited")
    monkeypatch.setattr(outcomes, "_fetch_tiingo", lambda _t: {})   # no token / no data
    monkeypatch.setattr(outcomes, "_fetch_yahoo", boom)
    monkeypatch.setattr(outcomes, "_fetch_stooq", lambda _t: {"2024-01-01": 50.0})
    assert outcomes.fetch_daily_closes("PGR") == {"2024-01-01": 50.0}


def test_fetch_daily_closes_empty_on_total_failure(monkeypatch):
    def boom(_t): raise RuntimeError("blocked")
    monkeypatch.setattr(outcomes, "_fetch_tiingo", boom)
    monkeypatch.setattr(outcomes, "_fetch_yahoo", boom)
    monkeypatch.setattr(outcomes, "_fetch_stooq", boom)
    assert outcomes.fetch_daily_closes("PGR") == {}


# ── discriminating label (the fix that makes the learned scorer trainable) ──


def test_followon_threshold_creates_label_balance(fresh_db, make_item, monkeypatch):
    monkeypatch.setattr(outcomes, "fetch_daily_closes", lambda t: {})
    ids = [_matured_item(make_item, "rss", f"i{i}", f"Plain item {i}", 40,
                         topic="cyber", scored=True) for i in range(4)]
    counts = {ids[0]: 100, ids[1]: 80, ids[2]: 5, ids[3]: 1}
    monkeypatch.setattr(outcomes, "_followon_count", lambda it, *a: counts[it["id"]])

    outcomes.run_outcomes(horizons=(7,))
    with db.get_conn() as conn:
        corr = {r["item_id"]: r["corroborated"] for r in conn.execute(
            "SELECT item_id, corroborated FROM outcome_backtest WHERE horizon_days=7")}
    # median([1,5,80,100]) → threshold 80: only the elevated two corroborate.
    assert corr[ids[0]] == 1 and corr[ids[1]] == 1
    assert corr[ids[2]] == 0 and corr[ids[3]] == 0


def test_regime_shift_alone_does_not_corroborate(fresh_db, make_item, monkeypatch):
    monkeypatch.setattr(outcomes, "fetch_daily_closes", lambda t: {})
    monkeypatch.setattr(outcomes, "_followon_count", lambda *a: 0)      # no follow-on
    monkeypatch.setattr(outcomes, "_regime_shifted", lambda s, e: True)  # regime shifts
    x = _matured_item(make_item, "rss", "x", "Plain item, no insurer", 40,
                      topic="cyber", scored=True)
    outcomes.run_outcomes(horizons=(7,))
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT corroborated, regime_shifted FROM outcome_backtest WHERE item_id=?",
            (x,)).fetchone()
    assert row["regime_shifted"] == 1        # recorded …
    assert row["corroborated"] == 0          # … but a window property doesn't corroborate


def test_edgar_item_resolves_ticker_from_source_id(fresh_db, make_item, monkeypatch):
    # An EDGAR title ('PGR 8-K') has no insurer NAME alias, so match_insurer would
    # miss it — the ticker must come from source_id ('PGR:acc') for stock/edgar to fire.
    monkeypatch.setattr(outcomes, "_followon_count", lambda *a: 0)
    monkeypatch.setattr(outcomes, "fetch_daily_closes", lambda t: {"d": 1.0})
    monkeypatch.setattr(outcomes, "compute_stock_move", lambda c, t, h: (2.5, "2+"))
    x = _matured_item(make_item, "edgar", "PGR:acc1", "PGR 8-K", 40,
                      topic="underwriting_results", scored=True)
    outcomes.run_outcomes(horizons=(7,))
    import json
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM outcome_backtest WHERE item_id=?", (x,)).fetchone()
    assert "stock_move" in json.loads(row["signals_json"])   # ticker resolved → signal fired
    assert row["stock_move_z"] == 2.5
