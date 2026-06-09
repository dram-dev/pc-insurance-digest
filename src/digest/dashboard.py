"""Phase C — Signal Desk dashboard + Home cockpit notes for the Obsidian vault.

Two generated `_meta/` notes:

- **Signal Desk** — a live cross-`Daily/` dashboard (DataviewJS over the enriched
  daily-note frontmatter: regime/vitals timeline) plus the market-wide visuals
  that don't fit the daily EKG header (reserve Sankey, catastrophe-season heatmap
  calendar) plus a calibration heatmap. The DataviewJS table re-queries on view;
  the big visuals are a SQLite snapshot taken at generation time.
- **Home** — a cockpit: a DataviewJS status strip, Buttons that drive the pipeline
  via Shell Commands, and quick links.

Reading them needs the **Dataview** plugin (+ **Heatmap Calendar**, **Buttons**,
**Shell Commands** for the richest blocks) — all in the user's approved cockpit
stack.

CLI: `uv run digest dashboard`
"""
from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from digest import viz_lab
from digest.config import settings


def _vault_root(start: Path) -> Path:
    """Obsidian vault root = nearest ancestor (incl. start) holding a `.obsidian`."""
    start = start.resolve()
    for cand in (start, *start.parents):
        if (cand / ".obsidian").is_dir():
            return cand
    return start


def _daily_source() -> str:
    """Dataview source for the daily-notes folder, relative to the Obsidian vault
    ROOT — which can sit ABOVE OBSIDIAN_VAULT_PATH (e.g. the digest writes to
    `vault_build/81 P&C Digest/` inside a vault rooted one level up). Dataview's
    `dv.pages("...")` resolves against the vault root, so the prefix matters."""
    vault_path = Path(settings.obsidian_vault_path)
    daily = (vault_path / settings.obsidian_digest_dir / "Daily").resolve()
    try:
        return daily.relative_to(_vault_root(vault_path)).as_posix()
    except ValueError:
        return f"{settings.obsidian_digest_dir}/Daily"


def build_return_watch() -> str:
    """Static 'Signal → Return Watch' table from the alpha engine: top predicted
    movers + the model's honest walk-forward scorecard. Rendered from SQLite at
    build time (forecasts don't live in note frontmatter, so Dataview can't see
    them). Degrades to a hint when no model has been trained yet."""
    from digest import db

    model = db.latest_return_model("excess_return")
    if model is None:
        return ("## 📈 Signal → Return Watch\n"
                "_No returns model yet — run `digest forecast prices` then "
                "`digest forecast train`._")

    def _f(x, sign=True):
        if x is None:
            return "—"
        return f"{x:+.4f}" if sign else f"{x:.4f}"

    from digest import alpha
    ic, base = model["ic"], model["baseline_ic"]
    edge = ("✅ real edge — positive IC, beats baselines" if alpha.has_edge(ic, base)
            else "⚠️ no edge — IC not positive / no lift over momentum (treat as noise)")
    lines = [
        "## 📈 Signal → Return Watch",
        f"*Local model #{model['id']} ({model['algo']}, {model['horizon_days']}d horizon) — "
        "predicts insurer excess return vs the IAK benchmark from the digest's own "
        "data + signal scores. **Advisory only**; never feeds the leaderboard.*",
        "",
        f"**Scorecard (out-of-sample):** IC {_f(ic)} · baseline {_f(base)} · "
        f"hit-rate {_f(model['hit_rate'], sign=False)} · "
        f"long-short {_f(model['long_short_ret'])} → {edge}",
        "",
    ]
    rows = db.latest_return_forecasts(horizon_days=model["horizon_days"], limit=14)
    if not rows:
        lines.append("_Model trained but no forecasts written — run `digest forecast predict`._")
        return "\n".join(lines)
    lines += ["| Insurer | Pred. excess | P(beat peer) | As of |", "|---|--:|--:|---|"]
    for r in rows:
        prob = f"{r['pred_prob']:.2f}" if r["pred_prob"] is not None else "—"
        lines.append(f"| {r['ticker']} | {_f(r['pred_excess'])} | {prob} | {r['as_of']} |")
    return "\n".join(lines)


def build_signal_desk_md() -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    src = _daily_source()
    return "\n".join([
        "---",
        "title: Signal Desk",
        "type: dashboard",
        "tags: [pc-digest, dashboard, _meta]",
        f"generated: {now}",
        "---",
        "",
        "# 🛰️ Signal Desk",
        f"*Live dashboard over the daily notes' frontmatter + market-wide visuals. "
        f"Generated {now}. Needs Dataview (+ Heatmap Calendar for the calendar).*",
        "",
        "## Regime & vitals timeline (last 30 daily notes)",
        "```dataviewjs",
        f"const pages = dv.pages('\"{src}\"')",
        "  .where(p => p.kind == \"digest-daily\")",
        "  .sort(p => p.date, 'desc').limit(30);",
        "if (!pages.length) {",
        "  dv.paragraph(\"_No daily notes with frontmatter yet — run `digest publish`._\");",
        "} else {",
        "  dv.table(",
        "    [\"Date\", \"Cycle\", \"CAT\", \"×mult\", \"Top\", \"Summ.\", \"Burden\", \"Docket/d\"],",
        "    pages.map(p => [p.date, p.regime_cycle ?? \"—\", p.cat_load ?? \"—\",",
        "      p.regime_mult ?? \"—\", p.top_score ?? \"—\", p.summarized_count ?? \"—\",",
        "      p.burden_top_state ?? \"—\", p.docket_velocity ?? \"—\"]));",
        "}",
        "```",
        "",
        "## Reserve-adequacy flows",
        viz_lab.render_reserve_sankey(),
        "",
        "## Catastrophe-season activity",
        viz_lab.render_cat_heatmap(),
        "",
        "## Calibration — system score vs your ratings",
        viz_lab.render_calibration_heatmap(),
        "",
        build_return_watch(),
        "",
        "---",
        "*Tip: rate items with `digest rate <id> <1-5>` to feed the calibration "
        "view; re-run `digest dashboard` to refresh the snapshot visuals.*",
        "",
    ])


def build_home_md() -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    src = _daily_source()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    proj = "~/Projects/pc-insurance-digest"
    buttons = [
        ("🔄 Full pipeline", "digest-pipeline"),
        ("📰 Brief", "digest-brief"),
        ("🏆 Signals", "digest-signals"),
        ("🛰️ Rebuild dashboard", "digest-dashboard"),
    ]
    button_blocks = "\n\n".join(
        f"```button\nname {label}\ntype command\naction Shell commands: {alias}\n```"
        for label, alias in buttons
    )
    setup_rows = "\n".join(
        f"| `{alias}` | `cd {proj} && uv run digest {alias.replace('digest-', '').replace('pipeline', 'pipeline --run-type manual')}` |"
        for _, alias in buttons
    )
    return "\n".join([
        "---",
        "title: Home",
        "type: cockpit",
        "tags: [pc-digest, cockpit, _meta]",
        f"generated: {now}",
        "---",
        "",
        "# 🏠 PC Digest — Cockpit",
        "",
        "## Status",
        "```dataviewjs",
        f"const p = dv.pages('\"{src}\"').where(x => x.kind == \"digest-daily\")",
        "  .sort(x => x.date, 'desc').first();",
        "if (p) dv.paragraph(`**${p.date}** · regime **${p.regime_cycle ?? \"?\"}** "
        "×${p.regime_mult ?? \"?\"} · **${p.summarized_count ?? 0}** summarized · "
        "top **${p.top_score ?? \"?\"}**`);",
        "else dv.paragraph(\"_No daily notes yet — run `digest pipeline`._\");",
        "```",
        "",
        "## Run the pipeline",
        "*Buttons need the **Buttons** + **Shell Commands** community plugins. "
        "Register each alias below in Shell Commands → Settings, then the buttons fire them.*",
        "",
        button_blocks,
        "",
        "### Shell Commands to register (one-time)",
        "| Alias | Command |",
        "|---|---|",
        setup_rows,
        "",
        "## Quick links",
        f"- [[Signal Desk]] · [[{today}]] (today) · [[Scoring Weights]] · "
        "[[PC Digest — Complete User Guide (2026-05-31)]] · [[Run Log]] · [[Mac Mini Tasks]]",
        "",
    ])


def write_dashboard(open_after: bool = False) -> list:
    """Write Signal Desk + Home into `_meta/`. Returns the paths written."""
    from digest.obsidian import Paths

    paths = Paths.resolve()
    meta = paths.digest_root / "_meta"
    meta.mkdir(parents=True, exist_ok=True)
    out = []
    for name, body in (("Signal Desk.md", build_signal_desk_md()),
                       ("Home.md", build_home_md())):
        p = meta / name
        p.write_text(body, encoding="utf-8")
        out.append(p)
    if open_after and sys.platform == "darwin":
        subprocess.run(["open", str(out[0])], check=False)
    return out
