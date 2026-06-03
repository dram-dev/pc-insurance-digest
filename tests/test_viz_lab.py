"""Viz Lab eval harness — renderer correctness + graceful-empty behavior.

Network-free: renderers read local SQLite. On an empty DB each renderer must
return a non-empty 'no data' note (the EKG leads are sparse until they mature),
and build_viz_lab_md() must always include every technique header.
"""
from __future__ import annotations

from datetime import datetime, timezone

from digest import db, viz_lab


# ── Unicode helpers ──────────────────────────────────────────────────────────

def test_braille_line_deterministic_and_packs_two_per_char():
    vals = [1, 2, 3, 4, 5, 6]
    a = viz_lab.braille_line(vals)
    b = viz_lab.braille_line(vals)
    assert a == b                       # deterministic
    assert len(a) == 3                  # 6 points → 3 braille chars (2/char)
    assert all(0x2800 <= ord(c) <= 0x28FF for c in a)   # braille block


def test_braille_line_empty():
    assert viz_lab.braille_line([]) == ""


def test_gauge_bar_clamps_and_fixed_width():
    assert viz_lab.gauge_bar(0.0) == "░" * 12
    assert viz_lab.gauge_bar(1.0) == "█" * 12
    assert viz_lab.gauge_bar(2.0) == "█" * 12   # clamped
    assert len(viz_lab.gauge_bar(0.37)) == 12


def test_regime_xy_mapping():
    assert viz_lab.regime_xy("hard_market", "post_major_event") == (0.90, 0.85)
    assert viz_lab.regime_xy("soft_market", "low_season") == (0.10, 0.15)
    assert viz_lab.regime_xy("stable", "active_season") == (0.50, 0.50)
    assert viz_lab.regime_xy("unknown", "unknown") == (0.5, 0.15)   # safe default


# ── Graceful-empty: every renderer returns a non-empty string ────────────────

def test_all_renderers_graceful_on_empty_db(fresh_db):
    for _, _, render in viz_lab._SECTIONS:
        out = render()
        assert isinstance(out, str) and out.strip(), f"{render.__name__} returned empty"


def test_build_viz_lab_md_includes_every_technique(fresh_db):
    md = viz_lab.build_viz_lab_md()
    assert md.startswith("---")                 # frontmatter
    assert "# Viz Lab" in md
    assert "## Scorecard" in md
    for title, _, _ in viz_lab._SECTIONS:
        assert f"## {title}" in md


# ── Populated: renderers reflect real data ───────────────────────────────────

def _put_regime(cycle="hard_market", cat="active_season", mult=1.32):
    db.upsert_regime_signal(
        as_of=datetime.now(timezone.utc).isoformat(),
        market_cycle=cycle, cat_load=cat,
        market_cycle_mult=1.2, cat_load_mult=1.1, multiplier=mult,
        evidence_json="{}", source="detector",
    )


def test_regime_quadrant_renders_now_point(fresh_db):
    _put_regime()
    out = viz_lab.render_regime_quadrant()
    assert "quadrantChart" in out
    assert '"Now": [0.90, 0.50]' in out
    assert "hard_market" in out


def test_burden_bars_render_state(fresh_db, make_item):
    db.upsert_items([make_item(source="legiscan", source_id="b1", title="CA bill",
                               metadata={"topic_hint": "regulatory_rate", "state": "CA"})])
    db.auto_keep_legiscan()
    out = viz_lab.render_burden_bars()
    assert "CA" in out and "█" in out


def test_burden_bars_unrated_intensity_is_neutral_not_green(fresh_db, make_item):
    """A state whose items have no burden_intensity must read ⬜ (unrated), not
    a misleading 🟩 'all-clear'."""
    db.upsert_items([make_item(source="legiscan", source_id="b1", title="CA bill",
                               metadata={"topic_hint": "regulatory_rate", "state": "CA"})])
    db.auto_keep_legiscan()
    out = viz_lab.render_burden_bars()
    assert "CA ⬜" in out
    assert "🟩" not in out


def test_burden_bars_high_intensity_is_red(fresh_db, make_item):
    """A high-intensity regulatory item drives the state's chip to 🟥."""
    db.upsert_items([make_item(source="state_doi", source_id="tx1", title="TX rate suppression",
                               metadata={"topic_hint": "regulatory_rate", "state": "TX"})])
    with db.get_conn() as c:
        c.execute("UPDATE items SET triage_decision='keep', topic='regulatory_rate', "
                  "state='TX', burden_intensity='high', burden_direction='increasing' "
                  "WHERE source_id='tx1'")
    out = viz_lab.render_burden_bars()
    assert "TX 🟥" in out


def test_write_viz_lab_writes_meta_note(fresh_db, tmp_path, monkeypatch):
    # Point the vault at a temp dir so the note lands somewhere writable.
    monkeypatch.setattr(viz_lab.settings, "obsidian_vault_path", str(tmp_path))
    out = viz_lab.write_viz_lab(open_after=False)
    assert out.exists()
    assert out.parent.name == "_meta"
    assert out.read_text(encoding="utf-8").startswith("---")
