"""Phase B — the Market EKG daily-note header panel (gated by EKG_HEADER_ENABLED)."""
from __future__ import annotations

from datetime import datetime, timezone

from digest import db, obsidian


def _put_regime():
    db.upsert_regime_signal(
        as_of=datetime.now(timezone.utc).isoformat(),
        market_cycle="hard_market", cat_load="active_season",
        market_cycle_mult=1.2, cat_load_mult=1.1, multiplier=1.32,
        evidence_json="{}", source="detector",
    )


def test_panel_empty_when_disabled(fresh_db, monkeypatch):
    monkeypatch.setattr(obsidian.settings, "ekg_header_enabled", False)
    _put_regime()
    assert obsidian._render_ekg_panel() == ""


def test_panel_renders_live_blocks_when_enabled(fresh_db, monkeypatch):
    monkeypatch.setattr(obsidian.settings, "ekg_header_enabled", True)
    _put_regime()
    panel = obsidian._render_ekg_panel()
    assert "## 🫀 Market EKG" in panel
    assert "**Market regime**" in panel and "quadrantChart" in panel
    assert "**Vital signs**" in panel        # regime mult gauge has data
    # Sparse leads (no data) must be skipped, not shown as "no data" noise.
    assert "No data yet" not in panel


def test_panel_empty_when_no_lead_data(fresh_db, monkeypatch):
    monkeypatch.setattr(obsidian.settings, "ekg_header_enabled", True)
    assert obsidian._render_ekg_panel() == ""   # enabled but nothing computed yet


def test_daily_note_gates_panel(fresh_db, monkeypatch):
    _put_regime()
    monkeypatch.setattr(obsidian.settings, "ekg_header_enabled", False)
    off, _ = obsidian.render_daily_note("2026-05-31")
    assert "## 🫀 Market EKG" not in off

    monkeypatch.setattr(obsidian.settings, "ekg_header_enabled", True)
    on, _ = obsidian.render_daily_note("2026-05-31")
    assert "## 🫀 Market EKG" in on
