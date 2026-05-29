"""Tests for the source catalog (the `digest sources` feature)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from digest_core import catalog
from digest_core.catalog import SourceSummary, sparkline


# ── sparkline ───────────────────────────────────────────────────────────


def test_sparkline_all_zero_is_flat_baseline():
    assert sparkline([0, 0, 0], width=7) == "▁" * 7


def test_sparkline_empty_is_flat_baseline():
    assert sparkline([], width=5) == "▁" * 5


def test_sparkline_peak_hits_top_tick():
    out = sparkline([0, 5, 10], width=3)
    assert out[-1] == "█"      # the peak
    assert out[0] == "▁"       # the zero
    assert len(out) == 3


def test_sparkline_right_aligns_and_pads():
    # fewer values than width → left-padded with baseline, newest stays right.
    out = sparkline([9], width=4)
    assert len(out) == 4
    assert out[:3] == "▁▁▁"
    assert out[3] == "█"


# ── SourceSummary.status ─────────────────────────────────────────────────


def _iso(hours_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


def test_status_never_run():
    assert SourceSummary(name="x").status == "never-run"


def test_status_import_failed_wins():
    s = SourceSummary(name="x", import_error="ImportError: no dep", last_run=_iso(1))
    assert s.status == "import-failed"


def test_status_active_recent():
    assert SourceSummary(name="x", last_run=_iso(2), last_status="ok").status == "active"


def test_status_stale_old():
    assert SourceSummary(name="x", last_run=_iso(72), last_status="ok").status == "stale"


def test_status_error_from_last_run():
    assert SourceSummary(name="x", last_run=_iso(1), last_status="error").status == "error"


# ── collect (against a real temp DB) ─────────────────────────────────────


def test_collect_enriches_registered_sources(fresh_db, make_item):
    from digest import db

    # Seed: two rss items + a run_log row for rss.
    db.upsert_items([make_item(source="rss", source_id="r1"),
                     make_item(source="rss", source_id="r2")])
    db.log_run(
        run_type="manual", source="rss", items_fetched=2, items_new=2,
        duration_ms=10, status="ok",
    )

    summaries, failures = catalog.collect(db.get_conn, "digest.ingest")
    by_name = {s.name: s for s in summaries}

    assert "rss" in by_name
    rss = by_name["rss"]
    assert rss.total_items == 2
    assert rss.last_new == 2
    assert rss.last_status == "ok"
    assert rss.status == "active"
    assert len(rss.daily_new) == 7            # default 7-day window
    assert sum(rss.daily_new) == 2            # today's bucket holds the 2 new

    # A registered-but-never-run source (no run_log row) reads as never-run.
    assert by_name["serff"].status == "never-run"
    assert by_name["serff"].total_items == 0
    assert isinstance(failures, dict)


def test_print_sources_smoke(fresh_db, capsys):
    from digest import db

    catalog.print_sources(db.get_conn, "digest.ingest")
    out = capsys.readouterr().out
    assert "Source catalog" in out
    assert "rss" in out
