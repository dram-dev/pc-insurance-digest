"""Triage window: "everything new since the previous run".

With one scheduled run a day, a fixed 24h window sits right on the cadence —
any drift in start time (PC queues behind macro) strands the previous run's
leftovers. The window instead reaches back past the previous scheduled run,
floored at TRIAGE_LOOKBACK_HOURS.
"""
from __future__ import annotations

from digest import db, triage
from digest_core.db import helpers as core_db


def _log_run_hours_ago(run_type: str, hours: float, source: str = "rss") -> None:
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO run_log (run_at, run_type, source, items_fetched, items_new, "
            "duration_ms, status) VALUES (datetime('now', ?), ?, ?, 0, 0, 0, 'ok')",
            (f"-{hours * 60:.0f} minutes", run_type, source),
        )


def _backdate_ingest(source_id: str, hours: float) -> None:
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE items SET ingested_at = datetime('now', ?) WHERE source_id = ?",
            (f"-{hours * 60:.0f} minutes", source_id),
        )


def test_no_scheduled_run_on_record_uses_the_floor(fresh_db):
    _log_run_hours_ago("manual", 100)  # manual/one-off ingests don't anchor
    assert db.triage_lookback_hours() == db.settings.triage_lookback_hours


def test_recent_previous_run_keeps_the_floor(fresh_db, monkeypatch):
    monkeypatch.setattr(db.settings, "triage_lookback_hours", 24)
    _log_run_hours_ago("pm", 12)
    assert db.triage_lookback_hours() == 24


def test_window_reaches_back_past_a_late_or_missed_run(fresh_db, monkeypatch):
    monkeypatch.setattr(db.settings, "triage_lookback_hours", 24)
    _log_run_hours_ago("daily", 25.5)  # started later than yesterday
    assert db.triage_lookback_hours() == 26 + 2
    _log_run_hours_ago("daily", 70, source="edgar")  # older rows don't matter
    assert db.triage_lookback_hours() == 28


def test_anchor_is_the_newest_scheduled_run(fresh_db):
    with db.get_conn() as conn:
        _log_run_hours_ago("daily", 72)
        _log_run_hours_ago("daily", 29.5)
        assert core_db.hours_since_previous_run(conn, 1, slack_hours=0) == 30


def test_previous_runs_leftover_is_triaged_despite_being_older_than_24h(
    fresh_db, make_item, monkeypatch
):
    monkeypatch.setattr(db.settings, "triage_lookback_hours", 24)
    # Yesterday's run ingested this item at 01:10 but hit its limit; today's run
    # starts 25h later — outside a fixed 24h window, inside since-previous-run.
    db.upsert_items([make_item(source_id="leftover", title="Leftover")])
    _backdate_ingest("leftover", 25)
    _log_run_hours_ago("daily", 24.8)

    hours = db.triage_lookback_hours()
    assert "Leftover" not in [r["title"] for r in db.items_needing_triage(limit=10)]
    assert "Leftover" in [
        r["title"] for r in db.items_needing_triage(limit=10, lookback_hours=hours)
    ]


def test_run_triage_uses_the_resolved_window_and_cap(fresh_db, monkeypatch):
    monkeypatch.setattr(triage.settings, "triage_max_per_run", 7)
    seen: dict = {}

    def _needing(limit, lookback_hours=None):
        seen["limit"], seen["lookback"] = limit, lookback_hours
        return []

    monkeypatch.setattr(triage.db, "items_needing_triage", _needing)
    monkeypatch.setattr(triage.db, "triage_lookback_hours", lambda: 31)
    triage.run_triage()
    assert seen == {"limit": 7, "lookback": 31}

    triage.run_triage(limit=3, lookback_hours=50)
    assert seen == {"limit": 3, "lookback": 50}
