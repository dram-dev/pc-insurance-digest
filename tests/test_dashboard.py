"""Phase C — Signal Desk dashboard + Home cockpit + calibration heatmap +
daily-note frontmatter enrichment."""
from __future__ import annotations

from datetime import datetime, timezone

from click.testing import CliRunner

from digest import cli, dashboard, db, obsidian, viz_lab


def _put_regime():
    db.upsert_regime_signal(
        as_of=datetime.now(timezone.utc).isoformat(),
        market_cycle="hard_market", cat_load="active_season",
        market_cycle_mult=1.2, cat_load_mult=1.1, multiplier=1.32,
        evidence_json="{}", source="detector",
    )


# ── Calibration heatmap ──────────────────────────────────────────────────────

def test_calibration_heatmap_empty(fresh_db):
    assert viz_lab.render_calibration_heatmap().lstrip().startswith("_No data yet")


def test_calibration_heatmap_populated(fresh_db, make_item):
    db.upsert_items([make_item(source="rss", source_id="c1", title="Rated item")])
    with db.get_conn() as c:
        iid = c.execute("SELECT id FROM items WHERE source_id='c1'").fetchone()["id"]
    db.upsert_manual_rating(iid, 4.0, note=None)
    # a computed score so system_score isn't NULL
    with db.get_conn() as c:
        c.execute(
            "INSERT INTO signal_scores (item_id, score, computed_at) VALUES (?,?,?)",
            (iid, 3.0, datetime.now(timezone.utc).isoformat()),
        )
    out = viz_lab.render_calibration_heatmap()
    assert "Rated item" in out
    assert "-1.00" in out          # Δ = system(3.0) − you(4.0)
    assert "mean |Δ|" in out


# ── Dashboard builders ───────────────────────────────────────────────────────

def test_signal_desk_has_all_sections(fresh_db):
    md = dashboard.build_signal_desk_md()
    assert md.startswith("---") and "# 🛰️ Signal Desk" in md
    assert "```dataviewjs" in md
    assert "Regime & vitals timeline" in md
    assert "Reserve-adequacy flows" in md
    assert "Catastrophe-season activity" in md
    assert "Calibration" in md
    # the dataview source points at the configured digest dir
    assert f"{dashboard.settings.obsidian_digest_dir}/Daily" in md


def test_home_has_status_and_buttons(fresh_db):
    md = dashboard.build_home_md()
    assert "# 🏠 PC Digest — Cockpit" in md
    assert "```dataviewjs" in md            # status strip
    assert "```button" in md                # at least one pipeline button
    assert "Shell Commands to register" in md
    assert "[[Signal Desk]]" in md


def test_write_dashboard_writes_two_meta_notes(fresh_db, tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard.settings, "obsidian_vault_path", str(tmp_path))
    paths = dashboard.write_dashboard(open_after=False)
    names = {p.name for p in paths}
    assert names == {"Signal Desk.md", "Home.md"}
    assert all(p.exists() and p.parent.name == "_meta" for p in paths)


# ── Weekly-job wiring ────────────────────────────────────────────────────────

def _patch_weekly(monkeypatch, calls):
    """Stub the weekly command's heavy collaborators so we can assert wiring."""
    monkeypatch.setattr(obsidian, "publish_weekly",
                        lambda date_iso=None: {"week": "2026-W22", "item_count": 0,
                                               "theme_count": 0, "path": "x"})
    monkeypatch.setattr(dashboard, "write_dashboard",
                        lambda *a, **k: calls.append("dashboard") or [])


def test_weekly_refreshes_dashboard_even_without_calibration(fresh_db, monkeypatch):
    """The dashboard refresh must NOT be gated behind --calibration."""
    calls: list[str] = []
    _patch_weekly(monkeypatch, calls)

    res = CliRunner().invoke(cli.main, ["weekly", "--no-calibration"])
    assert res.exit_code == 0
    assert calls == ["dashboard"]               # ran despite --no-calibration
    assert "dashboard refresh" in res.output
    assert "calibration loop" not in res.output  # calibration correctly skipped


def test_weekly_runs_calibration_then_dashboard(fresh_db, monkeypatch):
    """Default weekly run does the calibration loop AND the dashboard refresh."""
    calls: list[str] = []
    _patch_weekly(monkeypatch, calls)
    from digest import learn as learn_mod
    from digest import outcomes
    monkeypatch.setattr(outcomes, "run_outcomes",
                        lambda *a, **k: calls.append("outcomes") or {})
    monkeypatch.setattr(learn_mod, "run_best", lambda *a, **k: calls.append("learn") or {})

    res = CliRunner().invoke(cli.main, ["weekly"])
    assert res.exit_code == 0
    # dashboard runs last, after the calibration collaborators
    assert calls == ["outcomes", "learn", "dashboard"]


# ── Frontmatter enrichment ───────────────────────────────────────────────────

def test_daily_frontmatter_enriched_with_regime(fresh_db):
    _put_regime()
    from digest.regime import current_regime
    text, _ = obsidian.render_daily_note("2026-05-31", regime=current_regime())
    head = text.split("---", 2)[1]            # the YAML frontmatter block
    assert "regime_cycle: hard_market" in head
    assert "regime_mult:" in head


# ── Alpha engine — Signal → Return Watch panel ───────────────────────────────

def test_return_watch_no_model(fresh_db):
    md = dashboard.build_return_watch()
    assert "No returns model yet" in md


def test_return_watch_renders_scorecard_and_forecasts(fresh_db):
    import json
    mid = db.save_return_model({
        "target": "excess_return", "horizon_days": 20, "algo": "histgb",
        "n_samples": 120, "ic": 0.08, "hit_rate": 0.56, "baseline_ic": 0.02,
        "long_short_ret": 0.015, "features_json": json.dumps(["x"]),
        "model_blob": b"x", "model_json": None, "metrics_json": "{}",
    })
    db.upsert_return_forecasts([
        {"ticker": "PGR", "as_of": "2026-06-08", "horizon_days": 20,
         "pred_excess": 0.03, "pred_prob": 0.61, "model_id": mid, "scored_at": "now"},
        {"ticker": "ALL", "as_of": "2026-06-08", "horizon_days": 20,
         "pred_excess": -0.01, "pred_prob": 0.40, "model_id": mid, "scored_at": "now"},
    ])
    md = dashboard.build_return_watch()
    assert "beats baselines" in md            # IC 0.08 > baseline 0.02
    assert "| PGR |" in md and "| ALL |" in md
    assert "+0.0300" in md                     # PGR predicted excess, signed
    # PGR (higher pred_excess) ranks above ALL
    assert md.index("| PGR |") < md.index("| ALL |")
